"""Détection des problèmes liés aux équipements dans NetBox."""
from django.db.models import Count, Exists, OuterRef


def get_devices_missing_primary_ip():
    from dcim.models import Device
    return (
        Device.objects
        .filter(primary_ip4=None, primary_ip6=None)
        .select_related('site', 'role', 'device_type__manufacturer', 'tenant')
        .order_by('name')
    )


def get_devices_missing_site():
    from dcim.models import Device
    return (
        Device.objects
        .filter(site=None)
        .select_related('role', 'device_type__manufacturer', 'tenant')
        .order_by('name')
    )


def get_devices_missing_role():
    from dcim.models import Device
    return (
        Device.objects
        .filter(role=None)
        .select_related('site', 'device_type__manufacturer', 'tenant')
        .order_by('name')
    )


def get_devices_missing_device_type():
    from dcim.models import Device
    return (
        Device.objects
        .filter(device_type=None)
        .select_related('site', 'role', 'tenant')
        .order_by('name')
    )


def get_vms_missing_cluster():
    from virtualization.models import VirtualMachine
    return (
        VirtualMachine.objects
        .filter(cluster=None)
        .select_related('site', 'role', 'tenant')
        .order_by('name')
    )


def get_duplicate_mac_detail():
    """
    MACs en double sur les interfaces physiques et/ou VM.
    NetBox 4.x : les adresses MAC sont dans le modèle MACAddress (dcim).
    MACAddress.mac_address  = valeur MAC
    MACAddress.interface    = FK vers Interface (nullable)
    MACAddress.vminterface  = FK vers VMInterface (nullable)
    """
    from dcim.models import MACAddress

    dup_mac_values = list(
        MACAddress.objects
        .values('mac_address')
        .annotate(n=Count('pk'))
        .filter(n__gt=1)
        .values_list('mac_address', flat=True)
    )

    result = []
    for mac_val in sorted(dup_mac_values, key=str):
        phys = [
            m.interface
            for m in MACAddress.objects
            .filter(mac_address=mac_val, interface__isnull=False)
            .select_related('interface__device')
        ]
        virt = [
            m.vminterface
            for m in MACAddress.objects
            .filter(mac_address=mac_val, vminterface__isnull=False)
            .select_related('vminterface__virtual_machine')
        ]
        result.append({'mac': str(mac_val), 'physical': phys, 'virtual': virt})
    return result


def get_interfaces_no_ip_qs():
    """
    Queryset complet des interfaces sans IP — pour la pagination côté serveur.
    Retourne Interface.objects.none() en cas d'erreur.
    """
    from dcim.models import Interface
    from ipam.models import IPAddress
    from django.contrib.contenttypes.models import ContentType
    try:
        iface_ct = ContentType.objects.get_for_model(Interface)
        has_ip = IPAddress.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id=OuterRef('pk'),
        )
        return (
            Interface.objects
            .annotate(has_ip=Exists(has_ip))
            .filter(has_ip=False, mgmt_only=False)
            .select_related('device__site')
            .order_by('device__name', 'name')
        )
    except Exception:
        return Interface.objects.none()


def get_interfaces_no_ip(max_results=500):
    """
    Interfaces physiques sans IP assignée (hors mgmt-only).
    Utilise Exists sur GenericFK pour la détection.
    """
    from dcim.models import Interface
    from ipam.models import IPAddress
    from django.contrib.contenttypes.models import ContentType

    try:
        iface_ct = ContentType.objects.get_for_model(Interface)
        has_ip = IPAddress.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id=OuterRef('pk'),
        )
        qs = (
            Interface.objects
            .annotate(has_ip=Exists(has_ip))
            .filter(has_ip=False, mgmt_only=False)
            .select_related('device__site')
            .order_by('device__name', 'name')
        )
        total = qs.count()
        return qs[:max_results], total
    except Exception:
        return [], 0


def count_all():
    """Comptages rapides pour le dashboard."""
    from dcim.models import Device

    no_ip = Device.objects.filter(primary_ip4=None, primary_ip6=None).count()
    no_site = Device.objects.filter(site=None).count()
    no_role = Device.objects.filter(role=None).count()

    try:
        from dcim.models import MACAddress
        dup_macs_count = (
            MACAddress.objects
            .values('mac_address')
            .annotate(n=Count('pk'))
            .filter(n__gt=1)
            .count()
        )
    except Exception:
        dup_macs_count = 0

    return {
        'devices_no_ip': no_ip,
        'devices_no_site': no_site,
        'devices_no_role': no_role,
        'macs_dup': dup_macs_count,
    }
