from django.urls import path
from . import views

app_name = 'netbox_device_cleaner'

urlpatterns = [
    path('',          views.DashboardView.as_view(),    name='dashboard'),
    path('vlans/',    views.VlanHealthView.as_view(),    name='vlans'),
    path('ips/',      views.IpHealthView.as_view(),      name='ips'),
    path('prefixes/', views.PrefixHealthView.as_view(),  name='prefixes'),
    path('devices/',  views.DeviceHealthView.as_view(),  name='devices'),
    path('orphans/',  views.OrphansView.as_view(),       name='orphans'),
    path('purge/',    views.PurgeView.as_view(),         name='purge'),
]
