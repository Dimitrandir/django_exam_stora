from django.urls import path
from . import views
from .views import SalesDeleteView, SalesDetailView, RefundDetailView

urlpatterns = [
    path('add/', views.sales_add, name='sale_add'),
    path('cancel/', views.cancel_sale, name='cancel_sale'),
    path('draft/save/', views.sales_draft_save, name='sale_draft_save'),
    path('switch-tab/', views.switch_sale_tab, name='switch_sale_tab'),
    path('pos-pins/add/', views.pos_pin_add, name='pos_pin_add'),
    path('pos-pins/remove/<int:pk>/', views.pos_pin_remove, name='pos_pin_remove'),
    path('log-removed-item/', views.log_removed_sale_item, name='log_removed_sale_item'),
    path('refund/', views.refund_find, name='refund_find'),
    path('refund/<int:pk>/new/', views.refund_new, name='refund_new'),
    path('refund/<int:pk>/', RefundDetailView.as_view(), name='refund_details'),
    path('<int:pk>/', SalesDetailView.as_view(), name='sale_details'),
    path('<int:pk>/fiscal-retry/', views.sale_fiscal_retry, name='sale_fiscal_retry'),
    path('delete/<int:pk>/', SalesDeleteView.as_view(), name='sale_delete'),
]