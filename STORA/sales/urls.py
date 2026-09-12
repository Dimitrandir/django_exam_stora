from django.urls import path
from . import views
from .views import SalesListView, SalesDeleteView, SalesDetailView

urlpatterns = [
    path('', SalesListView.as_view(), name='sales_list'),
    path('add/', views.sales_add, name='sale_add'),
    path('draft/save/', views.sales_draft_save, name='sale_draft_save'),
    path('pos-pins/add/', views.pos_pin_add, name='pos_pin_add'),
    path('pos-pins/remove/<int:pk>/', views.pos_pin_remove, name='pos_pin_remove'),
    path('log-removed-item/', views.log_removed_sale_item, name='log_removed_sale_item'),
    path('<int:pk>/', SalesDetailView.as_view(), name='sale_details'),
    path('delete/<int:pk>/', SalesDeleteView.as_view(), name='sale_delete'),
]