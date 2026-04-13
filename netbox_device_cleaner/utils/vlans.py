"""Détection des problèmes de VLANs dans NetBox."""
from django.db.models import Count


def _conflict_type(vlans):
    """
    Retourne le type de conflit pour un groupe de VLANs, ou None si aucun conflit.

    Règles par paire :
    - Tenant absent sur l'un → 'tenant' (impossible de déterminer l'isolation).
    - Même tenant + pas de préfixes ou préfixes qui se chevauchent → 'tenant'.
    - Même tenant + préfixes distincts non-chevauchants → pas de conflit (VLANs légitimes).
    - Tenants différents + préfixes qui se chevauchent → 'ip' (conflit réseau).
    - Tenants différents + préfixes distincts → pas de conflit.
    """
    if len(vlans) <= 1:
        return None

    tenant_ids = [v.tenant_id for v in vlans]

    # Tenant absent sur au moins un VLAN → ambiguïté → doublon
    if None in tenant_ids:
        return 'tenant'

    # Construire les ensembles de préfixes pour chaque VLAN (prefetch_related actif)
    prefix_sets = [set(str(p.prefix) for p in v.prefixes.all()) for v in vlans]

    ip_conflict = False

    for i in range(len(vlans)):
        for j in range(i + 1, len(vlans)):
            same_tenant = (vlans[i].tenant_id == vlans[j].tenant_id)
            pi, pj     = prefix_sets[i], prefix_sets[j]

            if same_tenant:
                # Même tenant sans préfixes renseignés → impossible de prouver l'isolation
                if not pi or not pj:
                    return 'tenant'
                # Même tenant, préfixes qui se chevauchent → vrai doublon
                if pi & pj:
                    return 'tenant'
                # Même tenant, préfixes distincts → isolation par plage IP, pas un doublon

            else:
                # Tenants différents : conflit uniquement si la plage IP se chevauche
                if pi & pj:
                    ip_conflict = True

    return 'ip' if ip_conflict else None


def _is_genuine_duplicate_group(vlans):
    return _conflict_type(vlans) is not None


def get_duplicate_vids():
    """Groupes de VLANs partageant le même (vid, group) — filtre large."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .values('vid', 'group')
        .annotate(n=Count('pk'))
        .filter(n__gt=1)
        .order_by('vid')
    )


def get_duplicate_vlan_detail():
    """
    Objets VLAN complets pour chaque groupe de vrais doublons.
    Exclut les groupes où des tenants différents utilisent des réseaux distincts.
    """
    from ipam.models import VLAN
    duplicates = get_duplicate_vids().values_list('vid', 'group')
    result = []
    for vid, group_id in duplicates:
        vlans = list(
            VLAN.objects
            .filter(vid=vid, group_id=group_id)
            .select_related('group', 'tenant', 'role')
            .prefetch_related('prefixes')
        )
        ctype = _conflict_type(vlans)
        if ctype is None:
            continue
        result.append({
            'vid': vid,
            'group_id': group_id,
            'vlans': vlans,
            'conflict_type': ctype,
        })
    return result


def get_unused_vlans():
    """VLANs sans aucune interface physique ni VM (tagged ou untagged)."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .annotate(
            tagged_iface_count=Count('interfaces_as_tagged', distinct=True),
            untagged_iface_count=Count('interfaces_as_untagged', distinct=True),
            tagged_vmiface_count=Count('vminterfaces_as_tagged', distinct=True),
            untagged_vmiface_count=Count('vminterfaces_as_untagged', distinct=True),
        )
        .filter(
            tagged_iface_count=0,
            untagged_iface_count=0,
            tagged_vmiface_count=0,
            untagged_vmiface_count=0,
        )
        .select_related('group', 'tenant', 'role')
        .order_by('vid')
    )


def get_vlans_without_group():
    """VLANs non rattachés à un VLANGroup."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .filter(group=None)
        .select_related('tenant', 'role')
        .order_by('vid')
    )


def get_vlans_without_site_or_group():
    """VLANs globaux (sans site ni groupe)."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .filter(site=None, group=None)
        .select_related('tenant', 'role')
        .order_by('vid')
    )


def suggest_vlan_groups():
    """
    Analyse les VLANs sans groupe et suggère des VLANGroups basés sur
    le site des équipements et le tenant du VLAN.

    Nom du groupe : "{site.name} {tenant.name}" (ex. "DC1 SPE").

    Quand un VLAN est sur plusieurs sites, le site majoritaire (le plus
    de détections) est retenu avec un avertissement dans le résultat.

    Returns:
        suggestions  : list[dict]  — groupes suggérés triés par nom
        unassignable : list[dict]  — VLANs sans aucun équipement/site détecté
    """
    from ipam.models import VLAN, VLANGroup
    from dcim.models import Interface
    from virtualization.models import VMInterface
    from collections import defaultdict, Counter

    vlans = list(
        VLAN.objects
        .filter(group=None)
        .select_related('tenant', 'role')
        .order_by('vid')
    )
    if not vlans:
        return [], []

    vlan_pks  = {v.pk for v in vlans}
    vlan_map  = {v.pk: v for v in vlans}

    # vlan_pk → Counter{site_pk: count}
    vlan_site_counts = defaultdict(Counter)
    # site_pk → Site object
    site_objs = {}

    def _add_site(vlan_pk, site):
        if site and vlan_pk in vlan_pks:
            vlan_site_counts[vlan_pk][site.pk] += 1
            site_objs[site.pk] = site

    # NetBox 4.4.6 : VLAN n'a plus de champ site direct.
    # Le site est détecté uniquement via les interfaces des équipements associés.

    # 1. Interfaces physiques – untagged_vlan
    for iface in (
        Interface.objects
        .filter(untagged_vlan_id__in=vlan_pks, device__site__isnull=False)
        .select_related('device__site')
    ):
        _add_site(iface.untagged_vlan_id, iface.device.site)

    # 2. Interfaces physiques – tagged_vlans (via table M2M)
    try:
        tagged_through = Interface.tagged_vlans.through
        for row in (
            tagged_through.objects
            .filter(vlan_id__in=vlan_pks)
            .select_related('interface__device__site')
            .filter(interface__device__site__isnull=False)
        ):
            _add_site(row.vlan_id, row.interface.device.site)
    except Exception:
        pass

    # 3. VMInterfaces – untagged_vlan
    for vmiface in (
        VMInterface.objects
        .filter(untagged_vlan_id__in=vlan_pks)
        .select_related('virtual_machine__site', 'virtual_machine__cluster__site')
    ):
        vm   = vmiface.virtual_machine
        site = vm.site if vm.site_id else (
            vm.cluster.site if vm.cluster_id and vm.cluster.site_id else None
        )
        _add_site(vmiface.untagged_vlan_id, site)

    # 4. VMInterfaces – tagged_vlans (via table M2M)
    try:
        vm_tagged_through = VMInterface.tagged_vlans.through
        for row in (
            vm_tagged_through.objects
            .filter(vlan_id__in=vlan_pks)
            .select_related('vminterface__virtual_machine__site',
                            'vminterface__virtual_machine__cluster__site')
        ):
            vm   = row.vminterface.virtual_machine
            site = vm.site if vm.site_id else (
                vm.cluster.site if vm.cluster_id and vm.cluster.site_id else None
            )
            _add_site(row.vlan_id, site)
    except Exception:
        pass

    # Construire les suggestions
    suggestions  = {}   # (site_pk, tenant_pk) → dict
    unassignable = []   # VLANs sans aucune détection

    for vlan in vlans:
        counter = vlan_site_counts.get(vlan.pk)

        if not counter:
            unassignable.append({'vlan': vlan})
            continue

        # Site majoritaire
        ranked        = counter.most_common()
        top_site_pk   = ranked[0][0]
        site          = site_objs[top_site_pk]
        other_sites   = [site_objs[pk] for pk, _ in ranked[1:] if pk in site_objs]
        multi_site    = bool(other_sites)

        tenant = vlan.tenant
        key    = (site.pk, tenant.pk if tenant else None)

        if key not in suggestions:
            group_name = f"{site.name} {tenant.name}" if tenant else site.name
            existing   = VLANGroup.objects.filter(name=group_name).first()
            suggestions[key] = {
                'group_name':     group_name,
                'site':           site,
                'site_id':        site.pk,
                'tenant':         tenant,
                'existing_group': existing,
                'vlans':          [],
            }
        suggestions[key]['vlans'].append({
            'vlan':        vlan,
            'multi_site':  multi_site,
            'other_sites': other_sites,
        })

    result = sorted(suggestions.values(), key=lambda x: x['group_name'])
    return result, unassignable


def count_all():
    """Comptages rapides pour le dashboard."""
    from ipam.models import VLAN

    # Vrais doublons : applique le filtre tenant+préfixe
    duplicates = len(get_duplicate_vlan_detail())

    unused = (
        VLAN.objects
        .annotate(
            tc=Count('interfaces_as_tagged', distinct=True),
            uc=Count('interfaces_as_untagged', distinct=True),
            tvc=Count('vminterfaces_as_tagged', distinct=True),
            uvc=Count('vminterfaces_as_untagged', distinct=True),
        )
        .filter(tc=0, uc=0, tvc=0, uvc=0)
        .count()
    )
    no_group = VLAN.objects.filter(group=None).count()
    global_vlans = VLAN.objects.filter(site=None, group=None).count()
    return {
        'vlans_dup': duplicates,
        'vlans_unused': unused,
        'vlans_no_group': no_group,
        'vlans_global': global_vlans,
    }


def merge_vlans(keep_pk, delete_pks):
    """
    Réassigne toutes les références d'interface des VLANs à supprimer
    vers le VLAN à conserver, puis supprime les doublons.
    Retourne le VLAN conservé.
    """
    from django.db import transaction
    from ipam.models import VLAN
    from dcim.models import Interface
    from virtualization.models import VMInterface

    keep = VLAN.objects.get(pk=keep_pk)
    with transaction.atomic():
        for pk in delete_pks:
            if not pk:
                continue
            if int(pk) == int(keep_pk):
                continue
            victim = VLAN.objects.get(pk=pk)
            # Réassigner les interfaces tagged
            for iface in Interface.objects.filter(tagged_vlans=victim):
                iface.tagged_vlans.remove(victim)
                iface.tagged_vlans.add(keep)
            for vmiface in VMInterface.objects.filter(tagged_vlans=victim):
                vmiface.tagged_vlans.remove(victim)
                vmiface.tagged_vlans.add(keep)
            # Réassigner les interfaces untagged
            Interface.objects.filter(untagged_vlan=victim).update(untagged_vlan=keep)
            VMInterface.objects.filter(untagged_vlan=victim).update(untagged_vlan=keep)
            victim.delete()
    return keep
