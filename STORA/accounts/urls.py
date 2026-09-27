from django.urls import path
from .views import (
    UserRegisterView,
    UserLoginView,
    UserLogoutView,
    EmployeeListView,
    EmployeeDetailView,
    EmployeeUpdateView,
    EmployeeDeleteView,
    CompanyProfileUpdateView,
)

urlpatterns = [
    path('register/', UserRegisterView.as_view(), name='register'),
    path('login/', UserLoginView.as_view(), name='login'),
    path('logout/', UserLogoutView.as_view(), name='logout'),

    path('', EmployeeListView.as_view(), name='employee_list'),
    path('<int:pk>/', EmployeeDetailView.as_view(), name='employee_details'),
    path('edit/<int:pk>/', EmployeeUpdateView.as_view(), name='employee_edit'),
    path('delete/<int:pk>/', EmployeeDeleteView.as_view(), name='employee_delete'),
    path('company-profile/', CompanyProfileUpdateView.as_view(), name='company_profile_edit'),
]