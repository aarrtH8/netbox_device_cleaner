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
            .select_related('group', 'tenant')
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
    """
    VLANs sans aucune interface physique ni VM (tagged ou untagged)
    et sans aucun préfixe associé.
    """
    from ipam.models import VLAN
    return (
        VLAN.objects
        .annotate(
            tagged_iface_count=Count('interfaces_as_tagged', distinct=True),
            untagged_iface_count=Count('interfaces_as_untagged', distinct=True),
            tagged_vmiface_count=Count('vminterfaces_as_tagged', distinct=True),
            untagged_vmiface_count=Count('vminterfaces_as_untagged', distinct=True),
            prefix_count=Count('prefixes', distinct=True),
        )
        .filter(
            tagged_iface_count=0,
            untagged_iface_count=0,
            tagged_vmiface_count=0,
            untagged_vmiface_count=0,
            prefix_count=0,
        )
        .select_related('group', 'tenant')
        .order_by('vid')
    )


def get_vlans_without_group():
    """VLANs non rattachés à un VLANGroup."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .filter(group=None)
        .select_related('tenant')
        .order_by('vid')
    )


def get_vlans_without_site_or_group():
    """VLANs globaux (sans site ni groupe)."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .filter(site=None, group=None)
        .select_related('tenant')
        .order_by('vid')
    )


def _extract_site_trigram(device_name):
    """
    Extrait le trigramme de site depuis le nom d'un équipement.

    Convention attendue : [PREFIX(3)][SITE(3)][TENANT(3)][ROLE(3)][NUM(2)][ENV(1)]
    Exemple : THSDC1SPEFWL03P  →  DC1

    Retourne None si le nom est trop court pour extraire le trigramme.
    """
    if device_name and len(device_name) >= 6:
        return device_name[3:6].upper()
    return None


def suggest_vlan_groups():
    """
    Analyse les VLANs sans groupe et suggère des VLANGroups basés sur
    le trigramme de site extrait du nom des équipements et le tenant du VLAN.

    Nom du groupe : "{trigram} {tenant.name}" (ex. "DC1 SPE").
    Le trigramme est extrait à la position [3:6] du nom d'équipement.

    Quand un VLAN est détecté sur plusieurs trigrammes différents, le
    trigramme majoritaire est retenu avec un avertissement visuel.

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
        .select_related('tenant')
        .order_by('vid')
    )
    if not vlans:
        return [], []

    vlan_pks = {v.pk for v in vlans}

    # vlan_pk → Counter{trigram: count}
    vlan_trigram_counts = defaultdict(Counter)
    # trigram → premier Site object rencontré (pour le scope du groupe)
    trigram_site = {}

    def _add_device(vlan_pk, site, device_name):
        if not site or vlan_pk not in vlan_pks:
            return
        trigram = _extract_site_trigram(device_name)
        if not trigram:
            # Fallback : utiliser le début du nom de site
            trigram = site.name[:6].upper()
        vlan_trigram_counts[vlan_pk][trigram] += 1
        if trigram not in trigram_site:
            trigram_site[trigram] = site

    # NetBox 4.4.6 : VLAN n'a plus de champ site direct.
    # Le site et le trigramme sont détectés via les interfaces des équipements.

    # 1. Interfaces physiques – untagged_vlan
    for iface in (
        Interface.objects
        .filter(untagged_vlan_id__in=vlan_pks, device__site__isnull=False)
        .select_related('device__site')
    ):
        _add_device(iface.untagged_vlan_id, iface.device.site, iface.device.name)

    # 2. Interfaces physiques – tagged_vlans (via table M2M)
    try:
        tagged_through = Interface.tagged_vlans.through
        for row in (
            tagged_through.objects
            .filter(vlan_id__in=vlan_pks)
            .select_related('interface__device__site')
            .filter(interface__device__site__isnull=False)
        ):
            _add_device(
                row.vlan_id,
                row.interface.device.site,
                row.interface.device.name,
            )
    except Exception:
        pass

    # 3. VMInterfaces – untagged_vlan
    for vmiface in (
        VMInterface.objects
        .filter(untagged_vlan_id__in=vlan_pks)
        .select_related('virtual_machine')
    ):
        vm = vmiface.virtual_machine
        try:
            site = vm.site
        except Exception:
            site = None
        _add_device(vmiface.untagged_vlan_id, site, vm.name)

    # 4. VMInterfaces – tagged_vlans (via table M2M)
    try:
        vm_tagged_through = VMInterface.tagged_vlans.through
        for row in (
            vm_tagged_through.objects
            .filter(vlan_id__in=vlan_pks)
            .select_related('vminterface__virtual_machine')
        ):
            vm = row.vminterface.virtual_machine
            try:
                site = vm.site
            except Exception:
                site = None
            _add_device(row.vlan_id, site, vm.name)
    except Exception:
        pass

    # Construire les suggestions
    suggestions  = {}   # (trigram, tenant_pk) → dict
    unassignable = []   # VLANs sans aucune détection

    for vlan in vlans:
        counter = vlan_trigram_counts.get(vlan.pk)

        if not counter:
            unassignable.append({'vlan': vlan})
            continue

        # Trigramme majoritaire
        ranked          = counter.most_common()
        top_trigram     = ranked[0][0]
        site            = trigram_site[top_trigram]
        other_trigrams  = [t for t, _ in ranked[1:] if t != top_trigram]
        multi_site      = bool(other_trigrams)

        tenant = vlan.tenant
        key    = (top_trigram, tenant.pk if tenant else None)

        if key not in suggestions:
            group_name = f"{top_trigram} {tenant.name}" if tenant else top_trigram
            existing   = VLANGroup.objects.filter(name=group_name).first()
            suggestions[key] = {
                'group_name':     group_name,
                'site_trigram':   top_trigram,
                'site':           site,
                'site_id':        site.pk,
                'tenant':         tenant,
                'existing_group': existing,
                'vlans':          [],
            }
        suggestions[key]['vlans'].append({
            'vlan':           vlan,
            'multi_site':     multi_site,
            'other_trigrams': other_trigrams,
            'name_conflict':  False,   # rempli ci-dessous pour les groupes existants
        })

    # Détecter les conflits de noms — deux cas :
    # 1. Nom déjà présent dans le groupe cible existant (groupe existant)
    # 2. Doublon de nom au sein du même batch : même nom, VIDs différents
    #    (ex : vid=3400 et vid=3406, tous deux group=None, même nom)
    #    → le 2ème serait refusé par la contrainte unique (group_id, name).
    for suggestion in suggestions.values():
        taken = set()
        if suggestion['existing_group']:
            taken = set(
                VLAN.objects
                .filter(group=suggestion['existing_group'])
                .values_list('name', flat=True)
            )
        seen_in_batch = set()
        for item in suggestion['vlans']:
            name = item['vlan'].name
            if name in taken or name in seen_in_batch:
                item['name_conflict'] = True
            else:
                seen_in_batch.add(name)

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
            pc=Count('prefixes', distinct=True),
        )
        .filter(tc=0, uc=0, tvc=0, uvc=0, pc=0)
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
