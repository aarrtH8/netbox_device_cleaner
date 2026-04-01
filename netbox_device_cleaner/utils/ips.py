"""Détection des problèmes d'adresses IP dans NetBox."""
import re
from django.db.models import Count, Exists, OuterRef

# Suffixe de membre de stack : nom se terminant par 1 à 3 chiffres + P/p
# Exemples : SW-CORE-01P, SW-ACCESS-02P, ROUTER-00P
_STACK_SUFFIX_RE = re.compile(r'^(.*\D)(\d{1,3}[Pp])$')


def _stack_base(device_name):
    """
    Retourne la racine du nom si l'équipement semble être membre d'un stack
    (nom se terminant par des chiffres + P/p), sinon None.
    Exemples :
      'SW-CORE-01P' → 'SW-CORE-'
      'SW-CORE-02P' → 'SW-CORE-'
      'ROUTER-01'   → None  (pas de P final)
      'SERVER-01'   → None
    """
    m = _STACK_SUFFIX_RE.match(device_name)
    return m.group(1) if m else None


def _is_stack_group(ips):
    """
    Retourne True si TOUS les doublons d'une IP sont assignés à des interfaces
    physiques d'équipements formant un stack (même base de nom, suffixes différents).
    Si une seule IP n'est pas sur un membre de stack identifié, retourne False.
    """
    from dcim.models import Interface
    from django.contrib.contenttypes.models import ContentType

    iface_ct = ContentType.objects.get_for_model(Interface)

    # Toutes les IPs doivent être assignées à des interfaces physiques
    for ip in ips:
        if ip.assigned_object_type_id != iface_ct.pk:
            return False
        if ip.assigned_object_id is None:
            return False

    # Récupérer les noms des équipements liés à ces interfaces
    iface_ids = [ip.assigned_object_id for ip in ips]
    device_names = list(
        Interface.objects
        .filter(pk__in=iface_ids)
        .select_related('device')
        .values_list('device__name', flat=True)
    )

    if len(device_names) != len(ips):
        return False

    # Vérifier que tous les équipements partagent la même base de nom stack
    bases = set()
    for name in device_names:
        if not name:
            return False
        base = _stack_base(name)
        if base is None:
            return False
        bases.add(base)

    # Un seul nom de base = même stack
    return len(bases) == 1


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
    """
    Objets IPAddress complets pour chaque groupe de doublons.
    Les groupes où tous les doublons appartiennent à des membres d'un même
    stack (même base de nom + suffixe numérique+P) sont automatiquement exclus.
    """
    from ipam.models import IPAddress
    dup_keys = get_duplicate_ips().values_list('address', 'vrf')
    result = []
    for address, vrf_id in dup_keys:
        ips = list(
            IPAddress.objects
            .filter(address=address, vrf_id=vrf_id)
            .select_related('vrf', 'tenant')
        )
        # Ignorer les doublons qui s'expliquent par un stack
        if _is_stack_group(ips):
            continue
        result.append({
            'address': str(address),
            'vrf_id': vrf_id,
            'ips': ips,
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
    # Note : le comptage brut inclut les stacks (légère surestimation acceptable)
    dup_count = get_duplicate_ips().count()
    orphan_count = IPAddress.objects.filter(assigned_object_id=None).count()
    _, outside_total = get_ips_outside_prefix(max_results=1)
    return {
        'ips_dup': dup_count,
        'ips_orphan': orphan_count,
        'ips_outside_prefix': outside_total,
    }
