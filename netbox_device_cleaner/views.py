"""IPAM Health Suite v2 — vues Django pour le plugin NetBox."""
import logging
from django.shortcuts import get_object_or_404, render
from django.views.generic import View
from django.http import JsonResponse
from django.db import transaction, IntegrityError
from django.core.paginator import Paginator
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count

from dcim.models import Device, Interface, Site, DeviceRole
from virtualization.models import VirtualMachine, VMInterface
from ipam.models import Service, IPAddress, VLAN, Prefix, VRF, Aggregate
from tenancy.models import Tenant

from .utils import vlans as vlan_utils
from .utils import ips as ip_utils
from .utils import prefixes as prefix_utils
from .utils import devices as device_utils
from .utils import orphans as orphan_utils

logger = logging.getLogger('netbox.plugins.netbox_device_cleaner')


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_sidebar_counts():
    """Comptages minimes pour les badges de la sidebar."""
    try:
        counts = {}
        counts.update(vlan_utils.count_all())
        counts.update(ip_utils.count_all())
        counts.update(prefix_utils.count_all())
        counts.update(device_utils.count_all())
        counts.update(orphan_utils.count_all())
        return counts
    except Exception:
        return {}


def _json_error(msg, status=400):
    return JsonResponse({'success': False, 'error': msg}, status=status)


def _json_ok(**kwargs):
    return JsonResponse({'success': True, **kwargs})


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

class DashboardView(View):
    template_name = 'netbox_device_cleaner/dashboard.html'

    def get(self, request):
        from django.urls import reverse
        counts = _get_sidebar_counts()
        # On ne compte pas les issues purement informationnelles dans le total
        info_keys = {'vlans_no_group', 'vlans_global', 'prefixes_no_vrf',
                     'devices_no_role', 'vrfs_empty', 'vrfs_no_rt'}
        total_issues = sum(v for k, v in counts.items() if k not in info_keys)
        return render(request, self.template_name, {
            'active_module': 'dashboard',
            'sidebar_counts': counts,
            'counts': counts,
            'total_issues': total_issues,
            'vlan_url':   reverse('plugins:netbox_device_cleaner:vlans'),
            'ip_url':     reverse('plugins:netbox_device_cleaner:ips'),
            'prefix_url': reverse('plugins:netbox_device_cleaner:prefixes'),
            'device_url': reverse('plugins:netbox_device_cleaner:devices'),
            'orphan_url': reverse('plugins:netbox_device_cleaner:orphans'),
        })


# ─────────────────────────────────────────────────────────────────────────────
# VLAN Health
# ─────────────────────────────────────────────────────────────────────────────

class VlanHealthView(View):
    template_name = 'netbox_device_cleaner/vlans.html'

    def _tab_counts(self):
        c = vlan_utils.count_all()
        return {
            'duplicates':    c['vlans_dup'],
            'unused':        c['vlans_unused'],
            'no_group':      c['vlans_no_group'],
            'no_site_group': c['vlans_global'],
        }

    def get(self, request):
        tab = request.GET.get('tab', 'duplicates')
        ctx = {
            'active_module': 'vlans',
            'sidebar_counts': _get_sidebar_counts(),
            'tab': tab,
            'tab_counts': self._tab_counts(),
        }
        if tab == 'duplicates':
            ctx['groups'] = vlan_utils.get_duplicate_vlan_detail()
        elif tab == 'unused':
            ctx['vlans'] = vlan_utils.get_unused_vlans()
        elif tab == 'no_group':
            ctx['vlans'] = vlan_utils.get_vlans_without_group()
        else:
            ctx['vlans'] = vlan_utils.get_vlans_without_site_or_group()
        return render(request, self.template_name, ctx)

    def post(self, request):
        action = request.POST.get('action', '')
        handlers = {
            'delete_vlan':        self._delete_vlan,
            'delete_vlans_bulk':  self._delete_vlans_bulk,
            'merge_vlan':         self._merge_vlan,
            'delete_unused_bulk': self._delete_unused_bulk,
        }
        if action not in handlers:
            return _json_error(f"Action inconnue : {action}")
        try:
            return handlers[action](request)
        except IntegrityError as e:
            return _json_error(f"Contrainte d'intégrité : {e}")
        except Exception as e:
            logger.exception("Erreur VlanHealthView action=%s", action)
            return _json_error(str(e))

    def _delete_vlan(self, request):
        vlan_id = request.POST.get('vlan_id')
        with transaction.atomic():
            vlan = get_object_or_404(VLAN, pk=vlan_id)
            name = str(vlan)
            vlan.delete()
        logger.info("VLAN supprimé : %s (pk=%s)", name, vlan_id)
        return _json_ok(message=f"VLAN {name} supprimé.")

    def _delete_vlans_bulk(self, request):
        ids = request.POST.getlist('vlan_ids[]')
        if not ids:
            return _json_error("Aucun VLAN sélectionné.")
        with transaction.atomic():
            deleted, _ = VLAN.objects.filter(pk__in=ids).delete()
        return _json_ok(deleted=deleted, message=f"{deleted} VLAN(s) supprimé(s).")

    def _merge_vlan(self, request):
        keep_id = request.POST.get('keep_id')
        delete_ids = request.POST.getlist('delete_ids[]')
        if not keep_id or not delete_ids:
            return _json_error("Paramètres manquants pour la fusion.")
        keep = vlan_utils.merge_vlans(keep_id, delete_ids)
        logger.info("Fusion VLANs : conservé pk=%s, supprimés %s", keep_id, delete_ids)
        return _json_ok(message=f"Fusion effectuée. VLAN conservé : {keep}.")

    def _delete_unused_bulk(self, request):
        with transaction.atomic():
            qs = vlan_utils.get_unused_vlans()
            count = qs.count()
            qs.delete()
        return _json_ok(deleted=count, message=f"{count} VLAN(s) inutilisé(s) supprimé(s).")


# ─────────────────────────────────────────────────────────────────────────────
# IP Health
# ─────────────────────────────────────────────────────────────────────────────

class IpHealthView(View):
    template_name = 'netbox_device_cleaner/ips.html'

    def _tab_counts(self):
        c = ip_utils.count_all()
        return {
            'duplicates':     c['ips_dup'],
            'orphans':        c['ips_orphan'],
            'outside_prefix': c['ips_outside_prefix'],
        }

    def get(self, request):
        tab = request.GET.get('tab', 'duplicates')
        ctx = {
            'active_module': 'ips',
            'sidebar_counts': _get_sidebar_counts(),
            'tab': tab,
            'tab_counts': self._tab_counts(),
        }
        if tab == 'duplicates':
            ctx['groups'] = ip_utils.get_duplicate_ip_detail()
        elif tab == 'orphans':
            qs = ip_utils.get_orphan_ips()
            paginator = Paginator(qs, 100)
            ctx['page_obj'] = paginator.get_page(request.GET.get('page', 1))
        else:
            ips, total = ip_utils.get_ips_outside_prefix(max_results=500)
            ctx['ips'] = ips
            ctx['total'] = total
            ctx['truncated'] = total > 500
        return render(request, self.template_name, ctx)

    def post(self, request):
        action = request.POST.get('action', '')
        handlers = {
            'delete_ip':       self._delete_ip,
            'delete_ips_bulk': self._delete_ips_bulk,
            'delete_orphans':  self._delete_orphans,
        }
        if action not in handlers:
            return _json_error(f"Action inconnue : {action}")
        try:
            return handlers[action](request)
        except IntegrityError as e:
            return _json_error(f"Contrainte d'intégrité : {e}")
        except Exception as e:
            logger.exception("Erreur IpHealthView action=%s", action)
            return _json_error(str(e))

    def _delete_ip(self, request):
        ip_id = request.POST.get('ip_id')
        with transaction.atomic():
            ip = get_object_or_404(IPAddress, pk=ip_id)
            addr = str(ip.address)
            ip.delete()
        return _json_ok(message=f"IP {addr} supprimée.")

    def _delete_ips_bulk(self, request):
        ids = request.POST.getlist('ip_ids[]')
        if not ids:
            return _json_error("Aucune IP sélectionnée.")
        with transaction.atomic():
            deleted, _ = IPAddress.objects.filter(pk__in=ids).delete()
        return _json_ok(deleted=deleted, message=f"{deleted} adresse(s) IP supprimée(s).")

    def _delete_orphans(self, request):
        with transaction.atomic():
            count = IPAddress.objects.filter(assigned_object_id=None).count()
            IPAddress.objects.filter(assigned_object_id=None).delete()
        return _json_ok(deleted=count, message=f"{count} IP(s) orpheline(s) supprimée(s).")


# ─────────────────────────────────────────────────────────────────────────────
# Prefix Health
# ─────────────────────────────────────────────────────────────────────────────

class PrefixHealthView(View):
    template_name = 'netbox_device_cleaner/prefixes.html'

    def _tab_counts(self):
        c = prefix_utils.count_all()
        return {
            'overlapping': c['prefixes_overlap'],
            'unused':      c['prefixes_unused'],
            'no_vrf':      c['prefixes_no_vrf'],
        }

    def get(self, request):
        tab = request.GET.get('tab', 'overlapping')
        ctx = {
            'active_module': 'prefixes',
            'sidebar_counts': _get_sidebar_counts(),
            'tab': tab,
            'tab_counts': self._tab_counts(),
        }
        if tab == 'overlapping':
            try:
                ctx['prefixes'] = list(prefix_utils.get_overlapping_prefixes())
            except Exception:
                ctx['prefixes'] = []
                ctx['lookup_error'] = True
        elif tab == 'unused':
            try:
                qs = prefix_utils.get_unused_prefixes()
                ctx['page_obj'] = Paginator(qs, 100).get_page(request.GET.get('page', 1))
            except Exception:
                ctx['page_obj'] = None
                ctx['lookup_error'] = True
        else:
            ctx['prefixes'] = list(prefix_utils.get_prefixes_without_vrf())
            ctx['vrfs'] = list(VRF.objects.all().order_by('name'))
        return render(request, self.template_name, ctx)

    def post(self, request):
        action = request.POST.get('action', '')
        handlers = {
            'delete_prefix':        self._delete_prefix,
            'delete_prefixes_bulk': self._delete_prefixes_bulk,
            'assign_vrf':           self._assign_vrf,
        }
        if action not in handlers:
            return _json_error(f"Action inconnue : {action}")
        try:
            return handlers[action](request)
        except IntegrityError as e:
            return _json_error(f"Contrainte d'intégrité : {e}")
        except Exception as e:
            logger.exception("Erreur PrefixHealthView action=%s", action)
            return _json_error(str(e))

    def _delete_prefix(self, request):
        prefix_id = request.POST.get('prefix_id')
        with transaction.atomic():
            p = get_object_or_404(Prefix, pk=prefix_id)
            name = str(p.prefix)
            p.delete()
        return _json_ok(message=f"Préfixe {name} supprimé.")

    def _delete_prefixes_bulk(self, request):
        ids = request.POST.getlist('prefix_ids[]')
        if not ids:
            return _json_error("Aucun préfixe sélectionné.")
        with transaction.atomic():
            deleted, _ = Prefix.objects.filter(pk__in=ids).delete()
        return _json_ok(deleted=deleted, message=f"{deleted} préfixe(s) supprimé(s).")

    def _assign_vrf(self, request):
        prefix_id = request.POST.get('prefix_id')
        vrf_id = request.POST.get('vrf_id')
        with transaction.atomic():
            p = get_object_or_404(Prefix, pk=prefix_id)
            vrf = get_object_or_404(VRF, pk=vrf_id)
            p.vrf = vrf
            p.save()
        return _json_ok(message=f"VRF «{vrf.name}» assignée au préfixe {p.prefix}.")


# ─────────────────────────────────────────────────────────────────────────────
# Device Health (lecture seule)
# ─────────────────────────────────────────────────────────────────────────────

class DeviceHealthView(View):
    template_name = 'netbox_device_cleaner/devices.html'

    def _tab_counts(self):
        c = device_utils.count_all()
        return {
            'no_primary_ip':  c['devices_no_ip'],
            'no_site':        c['devices_no_site'],
            'no_role':        c['devices_no_role'],
            'dup_macs':       c['macs_dup'],
            'no_device_type': Device.objects.filter(device_type=None).count(),
            'no_cluster':     VirtualMachine.objects.filter(cluster=None).count(),
            'iface_no_ip':    0,
        }

    def get(self, request):
        tab = request.GET.get('tab', 'no_primary_ip')
        counts = self._tab_counts()
        ctx = {
            'active_module': 'devices',
            'sidebar_counts': _get_sidebar_counts(),
            'tab': tab,
            'tab_counts': counts,
        }
        if tab == 'no_primary_ip':
            ctx['devices'] = device_utils.get_devices_missing_primary_ip()
        elif tab == 'no_site':
            ctx['devices'] = device_utils.get_devices_missing_site()
        elif tab == 'no_role':
            ctx['devices'] = device_utils.get_devices_missing_role()
        elif tab == 'no_device_type':
            ctx['devices'] = device_utils.get_devices_missing_device_type()
        elif tab == 'no_cluster':
            ctx['vms'] = device_utils.get_vms_missing_cluster()
        elif tab == 'dup_macs':
            ctx['mac_groups'] = device_utils.get_duplicate_mac_detail()
        elif tab == 'iface_no_ip':
            ifaces, total = device_utils.get_interfaces_no_ip(max_results=500)
            ctx['ifaces'] = ifaces
            ctx['total'] = total
            ctx['truncated'] = total > 500
            counts['iface_no_ip'] = total
            ctx['tab_counts'] = counts
        return render(request, self.template_name, ctx)


# ─────────────────────────────────────────────────────────────────────────────
# Orphans
# ─────────────────────────────────────────────────────────────────────────────

class OrphansView(View):
    template_name = 'netbox_device_cleaner/orphans.html'

    def _tab_counts(self):
        c = orphan_utils.count_all()
        return {
            'services':   c['services_orphan'],
            'vrfs_empty': c['vrfs_empty'],
            'vrfs_no_rt': c['vrfs_no_rt'],
            'aggregates': Aggregate.objects.count(),
        }

    def get(self, request):
        tab = request.GET.get('tab', 'services')
        ctx = {
            'active_module': 'orphans',
            'sidebar_counts': _get_sidebar_counts(),
            'tab': tab,
            'tab_counts': self._tab_counts(),
        }
        if tab == 'services':
            ctx['services'] = orphan_utils.get_orphan_services()
        elif tab == 'vrfs_empty':
            ctx['vrfs'] = orphan_utils.get_empty_vrfs()
        elif tab == 'vrfs_no_rt':
            ctx['vrfs'] = orphan_utils.get_vrfs_without_route_targets()
        else:
            ctx['aggregates'] = orphan_utils.get_aggregates_without_prefixes()
        return render(request, self.template_name, ctx)

    def post(self, request):
        action = request.POST.get('action', '')
        handlers = {
            'delete_service':       self._delete_service,
            'delete_services_bulk': self._delete_services_bulk,
            'delete_vrf':           self._delete_vrf,
            'delete_vrfs_bulk':     self._delete_vrfs_bulk,
        }
        if action not in handlers:
            return _json_error(f"Action inconnue : {action}")
        try:
            return handlers[action](request)
        except IntegrityError as e:
            return _json_error(f"Contrainte d'intégrité : {e}")
        except Exception as e:
            logger.exception("Erreur OrphansView action=%s", action)
            return _json_error(str(e))

    def _delete_service(self, request):
        svc_id = request.POST.get('service_id')
        with transaction.atomic():
            svc = get_object_or_404(Service, pk=svc_id)
            name = svc.name
            svc.delete()
        return _json_ok(message=f"Service «{name}» supprimé.")

    def _delete_services_bulk(self, request):
        ids = request.POST.getlist('service_ids[]')
        if not ids:
            return _json_error("Aucun service sélectionné.")
        with transaction.atomic():
            deleted, _ = Service.objects.filter(pk__in=ids).delete()
        return _json_ok(deleted=deleted, message=f"{deleted} service(s) supprimé(s).")

    def _delete_vrf(self, request):
        vrf_id = request.POST.get('vrf_id')
        with transaction.atomic():
            vrf = get_object_or_404(VRF, pk=vrf_id)
            if Prefix.objects.filter(vrf=vrf).exists() or IPAddress.objects.filter(vrf=vrf).exists():
                return _json_error("Ce VRF n'est pas vide — suppression refusée.")
            name = vrf.name
            vrf.delete()
        return _json_ok(message=f"VRF «{name}» supprimé.")

    def _delete_vrfs_bulk(self, request):
        ids = request.POST.getlist('vrf_ids[]')
        if not ids:
            return _json_error("Aucun VRF sélectionné.")
        # Validation AVANT la transaction pour éviter un rollback partiel
        for vrf in VRF.objects.filter(pk__in=ids):
            if Prefix.objects.filter(vrf=vrf).exists() or IPAddress.objects.filter(vrf=vrf).exists():
                return _json_error(f"VRF «{vrf.name}» n'est pas vide — opération annulée.")
        with transaction.atomic():
            deleted, _ = VRF.objects.filter(pk__in=ids).delete()
        return _json_ok(deleted=deleted, message=f"{deleted} VRF(s) supprimé(s).")


# ─────────────────────────────────────────────────────────────────────────────
# Purge (fonctionnalité existante — améliorée)
# ─────────────────────────────────────────────────────────────────────────────

def _purge_object(kind, pk, delete_interfaces):
    """
    Supprime tous les objets NetBox liés à un device ou une VM.

    Supprime toujours :
      - Services (GenericFK)
      - IPAddresses assignées aux interfaces de l'équipement
      - VLANs utilisés EXCLUSIVEMENT par cet équipement (partagés = ignorés)

    Supprime si delete_interfaces=True :
      - Interfaces / VMInterfaces
    """
    if kind == 'vm':
        obj       = VirtualMachine.objects.get(pk=pk)
        iface_ct  = ContentType.objects.get_for_model(VMInterface)
        ifaces_qs = VMInterface.objects.filter(virtual_machine=obj)
    else:
        obj       = Device.objects.get(pk=pk)
        iface_ct  = ContentType.objects.get_for_model(Interface)
        ifaces_qs = Interface.objects.filter(device=obj)

    result = {
        'name': obj.name,
        'kind': kind,
        'services': 0,
        'ips': 0,
        'vlans': 0,
        'vlans_skipped': 0,
        'interfaces': 0,
    }

    # ── 1. Services ──────────────────────────────────────────────────
    if kind == 'vm':
        svc_qs = Service.objects.filter(virtual_machine=obj)
    else:
        svc_qs = Service.objects.filter(device=obj)
    result['services'] = svc_qs.count()
    svc_qs.delete()

    # ── 2. IPs assignées aux interfaces ──────────────────────────────
    iface_ids = list(ifaces_qs.values_list('id', flat=True))
    if iface_ids:
        ip_qs = IPAddress.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id__in=iface_ids,
        )
        result['ips'] = ip_qs.count()
        ip_qs.delete()

    # ── 3. VLANs exclusifs ──────────────────────────────────────────
    if iface_ids:
        tagged_ids = set(ifaces_qs.values_list('tagged_vlans', flat=True)) - {None}
        untagged_ids = set(
            ifaces_qs.exclude(untagged_vlan=None).values_list('untagged_vlan_id', flat=True)
        )
        all_vlan_ids = tagged_ids | untagged_ids

        for vlan_id in all_vlan_ids:
            if kind == 'vm':
                other = (
                    Interface.objects.filter(tagged_vlans=vlan_id).exists() or
                    Interface.objects.filter(untagged_vlan_id=vlan_id).exists() or
                    VMInterface.objects.filter(tagged_vlans=vlan_id).exclude(virtual_machine=obj).exists() or
                    VMInterface.objects.filter(untagged_vlan_id=vlan_id).exclude(virtual_machine=obj).exists()
                )
            else:
                other = (
                    Interface.objects.filter(tagged_vlans=vlan_id).exclude(device=obj).exists() or
                    Interface.objects.filter(untagged_vlan_id=vlan_id).exclude(device=obj).exists() or
                    VMInterface.objects.filter(tagged_vlans=vlan_id).exists() or
                    VMInterface.objects.filter(untagged_vlan_id=vlan_id).exists()
                )
            if other:
                result['vlans_skipped'] += 1
            else:
                VLAN.objects.filter(pk=vlan_id).delete()
                result['vlans'] += 1

    # ── 4. Interfaces (optionnel) ────────────────────────────────────
    if delete_interfaces and iface_ids:
        result['interfaces'] = ifaces_qs.count()
        ifaces_qs.delete()

    logger.info(
        '[Cleaner] %s: %d services, %d IPs, %d VLANs supprimés '
        '(%d partagés ignorés), %d interfaces',
        obj.name, result['services'], result['ips'],
        result['vlans'], result['vlans_skipped'], result['interfaces']
    )
    return result


class PurgeView(View):
    template_name = 'netbox_device_cleaner/purge.html'

    def get(self, request):
        devices = (
            Device.objects
            .select_related('site', 'role', 'device_type__manufacturer', 'tenant')
            .annotate(ip_count=Count('interfaces', distinct=True))
            .order_by('name')
        )
        vms = (
            VirtualMachine.objects
            .select_related('site', 'cluster', 'role', 'tenant')
            .annotate(ip_count=Count('interfaces', distinct=True))
            .order_by('name')
        )
        return render(request, self.template_name, {
            'active_module': 'purge',
            'sidebar_counts': _get_sidebar_counts(),
            'devices': devices,
            'vms': vms,
            'sites':   Site.objects.all().order_by('name'),
            'roles':   DeviceRole.objects.all().order_by('name'),
            'tenants': Tenant.objects.all().order_by('name'),
            'total': devices.count() + vms.count(),
        })

    def post(self, request):
        items = request.POST.getlist('items[]') or request.POST.getlist('items')
        delete_interfaces = request.POST.get('delete_interfaces') == '1'

        if not items:
            return JsonResponse({'success': False, 'error': 'Aucun équipement sélectionné.'})

        results = []
        errors = []
        for item in items:
            try:
                parts = item.split('_', 1)
                if len(parts) != 2:
                    errors.append({'item': item, 'error': 'Format invalide (attendu : kind_pk)'})
                    continue
                kind, pk_str = parts
                if kind not in ('device', 'vm'):
                    errors.append({'item': item, 'error': f'Type inconnu : {kind}'})
                    continue
                pk = int(pk_str)
                with transaction.atomic():
                    results.append(_purge_object(kind, pk, delete_interfaces))
            except Exception as e:
                errors.append({'item': item, 'error': str(e)})
                logger.error('[Cleaner] %s: %s', item, e)

        return JsonResponse({'success': True, 'results': results, 'errors': errors})
