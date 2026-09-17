from django.contrib import admin
from django.urls import path, include
from STORA.products.views import (index, custom_404)
from STORA.core.views import clear_cashier_operation, GlobalSearchView

urlpatterns = [
    path('', index, name='index'),
    path('admin/', admin.site.urls),
    path('products/', include('STORA.products.urls')),
    path('accounts/', include('STORA.accounts.urls')),
    path('sales/', include('STORA.sales.urls')),
    path('deliveries/', include('STORA.deliveries.urls')),
    path('reports/', include('STORA.reports.urls')),
    path('revisions/', include('STORA.revisions.urls')),
    path('core/clear-operation/', clear_cashier_operation, name='clear_cashier_operation'),
    path('search/', GlobalSearchView.as_view(), name='global_search'),
]

handler404 = 'STORA.products.views.custom_404'