"""Détection des problèmes d'adresses IP dans NetBox."""
from django.db.models import Count, Exists, OuterRef


def get_duplicate_ips():
    """Adresses IP avec le même couple (address, vrf)."""
    from ipam.models import IPAddress
    return (
        IPAddress.objects
        .values('address', 'vrf')
        .annotate(n=Count('pk'))
        .filter(n__gt=1)
        .order_by('address')
    )


def get_duplicate_ip_detail():
    """Objets IPAddress complets pour chaque groupe de doublons."""
    from ipam.models import IPAddress
    dup_keys = get_duplicate_ips().values_list('address', 'vrf')
    result = []
    for address, vrf_id in dup_keys:
        ips = (
            IPAddress.objects
            .filter(address=address, vrf_id=vrf_id)
            .select_related('vrf', 'tenant')
        )
        result.append({
            'address': str(address),
            'vrf_id': vrf_id,
            'ips': list(ips),
        })
    return result


def get_orphan_ips():
    """IPs non assignées à une interface (GenericFK non renseigné)."""
    from ipam.models import IPAddress
    return (
        IPAddress.objects
        .filter(assigned_object_id=None)
        .select_related('vrf', 'tenant')
        .order_by('address')
    )


def get_ips_outside_prefix(max_results=500):
    """
    IPs qui ne tombent dans aucun préfixe défini (même VRF).
    Utilise __net_contains_or_equals disponible via les lookups NetBox/netaddr.
    """
    from ipam.models import IPAddress, Prefix
    try:
        prefix_contains = Prefix.objects.filter(
            prefix__net_contains_or_equals=OuterRef('address'),
            vrf=OuterRef('vrf'),
        )
        qs = (
            IPAddress.objects
            .annotate(has_prefix=Exists(prefix_contains))
            .filter(has_prefix=False)
            .select_related('vrf', 'tenant')
            .order_by('address')
        )
        total = qs.count()
        return qs[:max_results], total
    except Exception:
        # Fallback si le lookup réseau n'est pas disponible
        return IPAddress.objects.none(), 0


def count_all():
    """Comptages rapides pour le dashboard."""
    from ipam.models import IPAddress
    dup_count = get_duplicate_ips().count()
    orphan_count = IPAddress.objects.filter(assigned_object_id=None).count()
    _, outside_total = get_ips_outside_prefix(max_results=1)
    return {
        'ips_dup': dup_count,
        'ips_orphan': orphan_count,
        'ips_outside_prefix': outside_total,
    }
