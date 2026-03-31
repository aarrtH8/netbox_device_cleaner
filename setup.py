from setuptools import setup, find_packages

setup(
    name='netbox_device_cleaner',
    version='2.0.0',
    description='IPAM Health Suite — détection et nettoyage des problèmes IPAM NetBox',
    author='Squad LAN DC',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[],
    package_data={
        'netbox_device_cleaner': [
            'templates/netbox_device_cleaner/*.html',
        ],
    },
    zip_safe=False,
)
