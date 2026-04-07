"""Détection des problèmes de VLANs dans NetBox."""
from django.db.models import Count


def _is_genuine_duplicate_group(vlans):
    """
    Détermine si un groupe de VLANs partageant le même VID est un vrai doublon.

    Règles :
    - Au moins un VLAN sans tenant → doublon (tenant non renseigné = cas ambigu).
    - Tous les tenants renseignés et tous différents → PAS un doublon (isolation par tenant).
    - Deux VLANs ou plus partagent le même tenant → doublon.
    """
    if len(vlans) <= 1:
        return False

    tenant_ids = [v.tenant_id for v in vlans]

    # Au moins un tenant absent → doublon
    if None in tenant_ids:
        return True

    # Tous différents → tenant-isolés, pas de problème
    if len(set(tenant_ids)) == len(tenant_ids):
        return False

    # Certains tenants sont partagés → doublon
    return True


def get_duplicate_vids():
    """Groupes de VLANs partageant le même (vid, group, site) — filtre large."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .values('vid', 'group', 'site')
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
    duplicates = get_duplicate_vids().values_list('vid', 'group', 'site')
    result = []
    for vid, group_id, site_id in duplicates:
        vlans = list(
            VLAN.objects
            .filter(vid=vid, group_id=group_id, site_id=site_id)
            .select_related('site', 'group', 'tenant', 'role')
        )
        if not _is_genuine_duplicate_group(vlans):
            continue
        result.append({
            'vid': vid,
            'group_id': group_id,
            'site_id': site_id,
            'vlans': vlans,
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
        .select_related('site', 'group', 'tenant', 'role')
        .order_by('vid')
    )


def get_vlans_without_group():
    """VLANs non rattachés à un VLANGroup."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .filter(group=None)
        .select_related('site', 'tenant', 'role')
        .order_by('vid')
    )


def get_vlans_without_site_or_group():
    """VLANs globaux (sans site ni groupe) — potentiellement orphelins."""
    from ipam.models import VLAN
    return (
        VLAN.objects
        .filter(site=None, group=None)
        .select_related('tenant', 'role')
        .order_by('vid')
    )


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
