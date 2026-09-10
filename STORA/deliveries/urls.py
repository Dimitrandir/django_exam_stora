from django.urls import path

from .api_views import DeliveryListAPI
from .views import (
    deliveries_add,
    delivery_edit,
    delivery_draft_save,
    DeliveryDetailView,
    DeliveryListView,
    DeliveryDeleteView,
    DocumentTypeListView,
    DocumentTypeCreateView,
    DocumentTypeUpdateView,
    DocumentTypeDeleteView,
)

urlpatterns = [
    path('', DeliveryListView.as_view(), name='deliveries_list'),
    path('add/', deliveries_add, name='delivery_add'),
    path('draft/save/', delivery_draft_save, name='delivery_draft_save'),
    path('document-types/', DocumentTypeListView.as_view(), name='document_type_list'),
    path('document-types/add/', DocumentTypeCreateView.as_view(), name='document_type_create'),
    path('document-types/edit/<int:pk>/', DocumentTypeUpdateView.as_view(), name='document_type_edit'),
    path('document-types/delete/<int:pk>/', DocumentTypeDeleteView.as_view(), name='document_type_delete'),
    path('<int:pk>/', DeliveryDetailView.as_view(), name='delivery_details'),
    path('edit/<int:pk>/', delivery_edit, name='delivery_edit'),
    path('delete/<int:pk>/', DeliveryDeleteView.as_view(), name='delivery_delete'),
    path('api/', DeliveryListAPI.as_view(), name='api_deliveries'),
]