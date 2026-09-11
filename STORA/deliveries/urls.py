from django.urls import path

from .api_views import DeliveryListAPI
from .models import DeliveryAttributes
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
    ScrapReasonListView,
    ScrapReasonCreateView,
    ScrapReasonUpdateView,
    ScrapReasonDeleteView,
    BatchSearchView,
)

urlpatterns = [
    path('', DeliveryListView.as_view(), name='deliveries_list'),
    path('add/', deliveries_add, name='delivery_add'),
    path('write-off/add/', deliveries_add, {'movement_type': DeliveryAttributes.MOVEMENT_WRITE_OFF}, name='writeoff_add'),
    path('scrap/add/', deliveries_add, {'movement_type': DeliveryAttributes.MOVEMENT_SCRAP}, name='scrap_add'),
    path('draft/save/', delivery_draft_save, name='delivery_draft_save'),
    path('document-types/', DocumentTypeListView.as_view(), name='document_type_list'),
    path('document-types/add/', DocumentTypeCreateView.as_view(), name='document_type_create'),
    path('document-types/edit/<int:pk>/', DocumentTypeUpdateView.as_view(), name='document_type_edit'),
    path('document-types/delete/<int:pk>/', DocumentTypeDeleteView.as_view(), name='document_type_delete'),
    path('scrap-reasons/', ScrapReasonListView.as_view(), name='scrap_reason_list'),
    path('scrap-reasons/add/', ScrapReasonCreateView.as_view(), name='scrap_reason_create'),
    path('scrap-reasons/edit/<int:pk>/', ScrapReasonUpdateView.as_view(), name='scrap_reason_edit'),
    path('scrap-reasons/delete/<int:pk>/', ScrapReasonDeleteView.as_view(), name='scrap_reason_delete'),
    path('batch-search/', BatchSearchView.as_view(), name='batch_search'),
    path('<int:pk>/', DeliveryDetailView.as_view(), name='delivery_details'),
    path('edit/<int:pk>/', delivery_edit, name='delivery_edit'),
    path('delete/<int:pk>/', DeliveryDeleteView.as_view(), name='delivery_delete'),
    path('api/', DeliveryListAPI.as_view(), name='api_deliveries'),
]