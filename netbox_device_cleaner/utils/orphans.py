"""Détection des objets orphelins dans NetBox (services, VRFs, agrégats)."""
from django.db.models import Count, Q


def get_orphan_services():
    """
    Services dont le parent (Device ou VM) n'existe plus.
    Utilise des sous-requêtes anti-join pour les deux types de parents.
    """
    from ipam.models import Service
    from dcim.models import Device
    from virtualization.models import VirtualMachine
    from django.contrib.contenttypes.models import ContentType

    device_ct = ContentType.objects.get_for_model(Device)
    vm_ct = ContentType.objects.get_for_model(VirtualMachine)

    device_ids = Device.objects.values('pk')
    vm_ids = VirtualMachine.objects.values('pk')

    return (
        Service.objects
        .filter(
            Q(parent_object_type=device_ct, parent_object_id__isnull=False) &
            ~Q(parent_object_id__in=device_ids)
            |
            Q(parent_object_type=vm_ct, parent_object_id__isnull=False) &
            ~Q(parent_object_id__in=vm_ids)
        )
        .select_related('parent_object_type')
        .order_by('name')
    )


def get_empty_vrfs():
    """VRFs sans préfixes ni adresses IP."""
    from ipam.models import VRF
    return (
        VRF.objects
        .annotate(
            prefix_count=Count('prefixes', distinct=True),
            ip_count=Count('ip_addresses', distinct=True),
        )
        .filter(prefix_count=0, ip_count=0)
        .select_related('tenant')
        .order_by('name')
    )


def get_vrfs_without_route_targets():
    """VRFs sans route targets (import ni export)."""
    from ipam.models import VRF
    return (
        VRF.objects
        .annotate(
            import_rt_count=Count('import_targets', distinct=True),
            export_rt_count=Count('export_targets', distinct=True),
        )
        .filter(import_rt_count=0, export_rt_count=0)
        .select_related('tenant')
        .order_by('name')
    )


def get_aggregates_without_prefixes():
    """Agrégats (blocs RIR) sans préfixe enfant."""
    from ipam.models import Aggregate, Prefix
    from django.db.models import Exists, OuterRef
    try:
        child_prefix = Prefix.objects.filter(
            prefix__net_contained_or_equals=OuterRef('prefix'),
        )
        return (
            Aggregate.objects
            .annotate(has_child=Exists(child_prefix))
            .filter(has_child=False)
            .select_related('rir', 'tenant')
            .order_by('prefix')
        )
    except Exception:
        return Aggregate.objects.none()


def count_all():
    """Comptages rapides pour le dashboard."""
    from ipam.models import VRF, Service
    from dcim.models import Device
    from virtualization.models import VirtualMachine
    from django.contrib.contenttypes.models import ContentType

    # Services orphelins
    try:
        orphan_svc_count = get_orphan_services().count()
    except Exception:
        orphan_svc_count = 0

    # VRFs vides
    empty_vrf_count = (
        VRF.objects
        .annotate(
            pc=Count('prefixes', distinct=True),
            ic=Count('ip_addresses', distinct=True),
        )
        .filter(pc=0, ic=0)
        .count()
    )

    # VRFs sans RT
    no_rt_count = (
        VRF.objects
        .annotate(
            irt=Count('import_targets', distinct=True),
            ert=Count('export_targets', distinct=True),
        )
        .filter(irt=0, ert=0)
        .count()
    )

    return {
        'services_orphan': orphan_svc_count,
        'vrfs_empty': empty_vrf_count,
        'vrfs_no_rt': no_rt_count,
    }
