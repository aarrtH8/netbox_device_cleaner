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
    """MACs en double sur les interfaces physiques et/ou VM."""
    from dcim.models import Interface
    from virtualization.models import VMInterface

    dup_phys = (
        Interface.objects
        .exclude(mac_address=None)
        .exclude(mac_address='')
        .values('mac_address')
        .annotate(n=Count('pk'))
        .filter(n__gt=1)
        .values_list('mac_address', flat=True)
    )
    dup_vm = (
        VMInterface.objects
        .exclude(mac_address=None)
        .exclude(mac_address='')
        .values('mac_address')
        .annotate(n=Count('pk'))
        .filter(n__gt=1)
        .values_list('mac_address', flat=True)
    )

    all_macs = set(list(dup_phys) + list(dup_vm))
    result = []
    for mac in sorted(all_macs):
        phys = list(Interface.objects.filter(mac_address=mac).select_related('device'))
        virt = list(VMInterface.objects.filter(mac_address=mac).select_related('virtual_machine'))
        result.append({'mac': str(mac), 'physical': phys, 'virtual': virt})
    return result


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
    from dcim.models import Device, Interface
    from virtualization.models import VirtualMachine
    from ipam.models import IPAddress
    from django.contrib.contenttypes.models import ContentType

    no_ip = Device.objects.filter(primary_ip4=None, primary_ip6=None).count()
    no_site = Device.objects.filter(site=None).count()
    no_role = Device.objects.filter(role=None).count()

    # MACs en double
    dup_macs_phys = (
        Interface.objects
        .exclude(mac_address=None).exclude(mac_address='')
        .values('mac_address').annotate(n=Count('pk')).filter(n__gt=1).count()
    )
    dup_macs_vm_qs = []
    try:
        from virtualization.models import VMInterface
        dup_macs_vm = (
            VMInterface.objects
            .exclude(mac_address=None).exclude(mac_address='')
            .values('mac_address').annotate(n=Count('pk')).filter(n__gt=1)
            .values_list('mac_address', flat=True)
        )
        # Count unique MACs in duplicate across both
        phys_macs = set(
            Interface.objects
            .exclude(mac_address=None).exclude(mac_address='')
            .values('mac_address').annotate(n=Count('pk')).filter(n__gt=1)
            .values_list('mac_address', flat=True)
        )
        vm_macs = set(dup_macs_vm)
        dup_macs_count = len(phys_macs | vm_macs)
    except Exception:
        dup_macs_count = dup_macs_phys

    return {
        'devices_no_ip': no_ip,
        'devices_no_site': no_site,
        'devices_no_role': no_role,
        'macs_dup': dup_macs_count,
    }
