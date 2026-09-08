from django.urls import path

from .api_views import ProductListAPI
from .views import (
    ProductListView, ProductCreateView, ProductDeleteView, ProductUpdateView, ProductDetailView,
    ProductHistoryView, ProductInlineUpdateView, ProductBulkActionView,
    CategoryListView, CategoryCreateView, CategoryDeleteView, CategoryUpdateView,
    SuppliersListView, SupplierCreateView, SupplierUpdateView, SupplierDeleteView
)

urlpatterns = [
    path('', ProductListView.as_view(), name='product_list'),
    path('create/', ProductCreateView.as_view(), name='product_create'),
    path('bulk-action/', ProductBulkActionView.as_view(), name='product_bulk_action'),
    path('<int:pk>/', ProductDetailView.as_view(), name='product_details'),
    path('<int:pk>/history/', ProductHistoryView.as_view(), name='product_history'),
    path('<int:pk>/inline-update/', ProductInlineUpdateView.as_view(), name='product_inline_update'),
    path('edit/<int:pk>/', ProductUpdateView.as_view(), name='product_edit'),
    path('delete/<int:pk>/', ProductDeleteView.as_view(), name='product_delete'),
    path('categories/', CategoryListView.as_view(), name='category_list'),
    path('categories/add/', CategoryCreateView.as_view(), name='category_create'),
    path('categories/edit/<int:pk>/', CategoryUpdateView.as_view(), name='category_edit'),
    path('categories/delete/<int:pk>/', CategoryDeleteView.as_view(), name='category_delete'),
    path('suppliers/', SuppliersListView.as_view(), name='suppliers_list'),
    path('suppliers/add/', SupplierCreateView.as_view(), name='suppliers_create'),
    path('suppliers/edit/<int:pk>/', SupplierUpdateView.as_view(), name='suppliers_edit'),
    path('suppliers/delete/<int:pk>/', SupplierDeleteView.as_view(), name='suppliers_delete'),
    path('api/products/', ProductListAPI.as_view(), name='api_products'),
]