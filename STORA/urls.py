from django.contrib import admin
from django.urls import path, include
from STORA.products.views import (index, custom_404)
from STORA.core.views import clear_cashier_operation, GlobalSearchView

urlpatterns = [
    path('', index, name='index'),
    path('admin/', admin.site.urls),
    # Django's built-in "set the active language, remember it, redirect
    # back" view (POST {language, next}) -- used by the BG/EN switcher in
    # base.html's navbar. Not under i18n_patterns (no /bg/, /en/ URL
    # prefix) -- the language is picked per-browser via a cookie, same
    # page URLs regardless of which one is active.
    path('i18n/', include('django.conf.urls.i18n')),
    path('products/', include('STORA.products.urls')),
    path('accounts/', include('STORA.accounts.urls')),
    path('sales/', include('STORA.sales.urls')),
    path('deliveries/', include('STORA.deliveries.urls')),
    path('reports/', include('STORA.reports.urls')),
    path('revisions/', include('STORA.revisions.urls')),
    path('pricelists/', include('STORA.pricelists.urls')),
    path('orders/', include('STORA.orders.urls')),
    path('core/clear-operation/', clear_cashier_operation, name='clear_cashier_operation'),
    path('search/', GlobalSearchView.as_view(), name='global_search'),
]

handler404 = 'STORA.products.views.custom_404'