from django.urls import path

from .views import (
    revision_home,
    revision_start,
    revision_view,
    revision_items_data,
    revision_add_item,
    revision_remove_item,
    revision_complete,
    revision_cancel,
    RevisionListView,
)

urlpatterns = [
    path('', revision_home, name='revision_home'),
    path('start/', revision_start, name='revision_start'),
    path('history/', RevisionListView.as_view(), name='revision_list'),
    path('<int:pk>/', revision_view, name='revision_view'),
    path('<int:pk>/items/', revision_items_data, name='revision_items_data'),
    path('<int:pk>/add-item/', revision_add_item, name='revision_add_item'),
    path('<int:pk>/remove-item/<int:item_id>/', revision_remove_item, name='revision_remove_item'),
    path('<int:pk>/complete/', revision_complete, name='revision_complete'),
    path('<int:pk>/cancel/', revision_cancel, name='revision_cancel'),
]
