from django.urls import path

from . import views
from .views import OrderDetailView, OrderListView

urlpatterns = [
    path('', OrderListView.as_view(), name='order_list'),
    path('new/', views.order_new, name='order_new'),
    path('ai-suggest/', views.order_ai_suggest, name='order_ai_suggest'),
    path('ai-draft-all/', views.order_ai_draft_all, name='order_ai_draft_all'),
    path('picker.json', views.order_picker_json, name='order_picker_json'),
    path('<int:pk>/', OrderDetailView.as_view(), name='order_details'),
    path('<int:pk>/edit/', views.order_edit, name='order_edit'),
    path('<int:pk>/confirm/', views.order_confirm, name='order_confirm'),
    path('<int:pk>/discard/', views.order_discard, name='order_discard'),
    path('<int:pk>/items.json', views.order_items_json, name='order_items_json'),
]
