"""IPAM Health Suite — détection et nettoyage des problèmes IPAM dans NetBox."""
from netbox.plugins import PluginConfig


class NetBoxDeviceCleanerConfig(PluginConfig):
    name         = 'netbox_device_cleaner'
    verbose_name = 'IPAM Health Suite'
    description  = (
        'Suite complète de santé IPAM : doublons VLANs/IPs, préfixes chevauchants, '
        'équipements incomplets, objets orphelins et purge d\'équipements'
    )
    version      = '2.0.0'
    author       = 'Squad LAN DC'
    base_url     = 'device-cleaner'
    min_version  = '4.0.0'
    max_version  = '4.9.99'


config = NetBoxDeviceCleanerConfig
