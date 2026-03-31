"""Détection des problèmes de préfixes dans NetBox."""
from django.db.models import Exists, OuterRef


def get_overlapping_prefixes():
    """
    Préfixes qui se chevauchent dans le même VRF (sans relation parent/enfant).
    Utilise __net_overlaps disponible via les lookups NetBox/PostgreSQL.
    """
    from ipam.models import Prefix
    try:
        overlap_qs = Prefix.objects.filter(
            vrf=OuterRef('vrf'),
            prefix__net_overlaps=OuterRef('prefix'),
        ).exclude(pk=OuterRef('pk'))

        qs = (
            Prefix.objects
            .annotate(has_overlap=Exists(overlap_qs))
            .filter(has_overlap=True)
            .select_related('vrf', 'site', 'tenant', 'role', 'vlan')
            .order_by('prefix')
        )
        return qs
    except Exception:
        return type('EmptyQS', (), {'count': lambda s: 0, '__iter__': lambda s: iter([])})()


def get_unused_prefixes():
    """
    Préfixes sans sous-préfixes ni adresses IP.
    """
    from ipam.models import Prefix, IPAddress
    try:
        child_prefix = Prefix.objects.filter(
            prefix__net_contained_by=OuterRef('prefix'),
            vrf=OuterRef('vrf'),
        ).exclude(pk=OuterRef('pk'))

        child_ip = IPAddress.objects.filter(
            address__net_contained_or_equals=OuterRef('prefix'),
            vrf=OuterRef('vrf'),
        )

        return (
            Prefix.objects
            .annotate(
                has_child_prefix=Exists(child_prefix),
                has_child_ip=Exists(child_ip),
            )
            .filter(has_child_prefix=False, has_child_ip=False)
            .select_related('vrf', 'site', 'tenant', 'role', 'vlan')
            .order_by('prefix')
        )
    except Exception:
        return type('EmptyQS', (), {'count': lambda s: 0, '__iter__': lambda s: iter([])})()


def get_prefixes_without_vrf():
    """Préfixes dans la table de routage globale quand des VRFs sont définies."""
    from ipam.models import Prefix, VRF
    if not VRF.objects.exists():
        return Prefix.objects.none()
    return (
        Prefix.objects
        .filter(vrf=None)
        .select_related('site', 'tenant', 'role', 'vlan')
        .order_by('prefix')
    )


def count_all():
    """Comptages rapides pour le dashboard."""
    from ipam.models import Prefix, VRF

    try:
        overlap_count = get_overlapping_prefixes().count()
    except Exception:
        overlap_count = 0

    try:
        unused_count = get_unused_prefixes().count()
    except Exception:
        unused_count = 0

    no_vrf_count = 0
    if VRF.objects.exists():
        no_vrf_count = Prefix.objects.filter(vrf=None).count()

    return {
        'prefixes_overlap': overlap_count,
        'prefixes_unused': unused_count,
        'prefixes_no_vrf': no_vrf_count,
    }
