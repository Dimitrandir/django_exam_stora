from django.urls import path

from .views import (
    PriceListListView,
    price_list_create,
    price_list_edit,
    price_list_detail,
    price_list_delete,
    price_list_check_conflict,
)

urlpatterns = [
    path('', PriceListListView.as_view(), name='price_list_list'),
    path('add/', price_list_create, name='price_list_create'),
    path('check-conflict/', price_list_check_conflict, name='price_list_check_conflict'),
    path('<int:pk>/', price_list_detail, name='price_list_detail'),
    path('<int:pk>/edit/', price_list_edit, name='price_list_edit'),
    path('<int:pk>/delete/', price_list_delete, name='price_list_delete'),
]
