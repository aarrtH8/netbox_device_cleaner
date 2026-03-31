try:
    # NetBox 3.4+ / 4.x
    from netbox.plugins.navigation import PluginMenu, PluginMenuItem
    menu = PluginMenu(
        label='IPAM Health Suite',
        groups=(
            ('Tableau de bord', (
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:dashboard',
                    link_text="Vue d'ensemble",
                ),
            )),
            ('Analyse IPAM', (
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:vlans',
                    link_text='Santé VLANs',
                ),
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:ips',
                    link_text='Santé Adresses IP',
                ),
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:prefixes',
                    link_text='Santé Préfixes',
                ),
            )),
            ('Équipements', (
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:devices',
                    link_text='Santé Équipements',
                ),
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:orphans',
                    link_text='Objets Orphelins',
                ),
            )),
            ('Actions', (
                PluginMenuItem(
                    link='plugins:netbox_device_cleaner:purge',
                    link_text='Purge Équipements',
                ),
            )),
        ),
        icon_class='mdi mdi-heart-pulse',
    )
except (ImportError, TypeError):
    # Fallback pour les versions antérieures
    from netbox.plugins import PluginMenuItem
    menu_items = (
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:dashboard',
            link_text="IPAM Health — Vue d'ensemble",
        ),
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:vlans',
            link_text='Santé VLANs',
        ),
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:ips',
            link_text='Santé Adresses IP',
        ),
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:prefixes',
            link_text='Santé Préfixes',
        ),
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:devices',
            link_text='Santé Équipements',
        ),
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:orphans',
            link_text='Objets Orphelins',
        ),
        PluginMenuItem(
            link='plugins:netbox_device_cleaner:purge',
            link_text='Purge Équipements',
        ),
    )
