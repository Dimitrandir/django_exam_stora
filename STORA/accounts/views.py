from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView, DetailView, UpdateView, DeleteView
from STORA.accounts.forms import CustomUserCreationForm
from STORA.accounts.models import Employee
from STORA.core.mixins import StaffPermissionRequiredMixin


class UserRegisterView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    # Only a Manager may add new employees -- registration is not public
    # self-signup, it's how a manager onboards staff.
    permission_required = 'accounts.add_employee'
    form_class = CustomUserCreationForm
    template_name = 'accounts/register.html'
    success_url = reverse_lazy('employee_list')


class UserLoginView(LoginView):
    template_name = 'accounts/login.html'


class UserLogoutView(LogoutView):
    next_page = reverse_lazy('index')


class EmployeeListView(LoginRequiredMixin, ListView):
    model = Employee
    template_name = 'accounts/employee_list.html'
    context_object_name = 'employees'


class EmployeeDetailView(LoginRequiredMixin, DetailView):
    model = Employee
    template_name = 'accounts/employee_details.html'
    context_object_name = 'employee'


class EmployeeUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    # Editing includes the `role` field, i.e. granting/revoking permissions --
    # restricted to Managers so a Cashier can't promote themselves.
    permission_required = 'accounts.change_employee'
    model = Employee
    template_name = 'accounts/employee_edit.html'
    fields = ['username', 'email', 'first_name', 'last_name', 'phone', 'role']
    context_object_name = 'employee'

    def get_success_url(self):
        return reverse_lazy('employee_details', kwargs={'pk': self.object.pk})


class EmployeeDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'accounts.delete_employee'
    model = Employee
    template_name = 'accounts/employee_confirm_delete.html'
    success_url = reverse_lazy('employee_list')
    context_object_name = 'employee'