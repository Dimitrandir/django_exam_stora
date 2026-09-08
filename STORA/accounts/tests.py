from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from STORA.accounts.models import Employee

User = get_user_model()


class AuthViewTests(TestCase):
    def test_register_page_requires_login(self):
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 302)

    def test_login_page_loads(self):
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)

    def test_logout_redirects(self):
        response = self.client.get(reverse('logout'))
        self.assertIn(response.status_code, (302, 405))

    def test_wrong_credentials_show_error_once_without_raw_field_name(self):
        response = self.client.post(
            reverse('login'), {'username': 'nobody', 'password': 'wrong'}
        )
        content = response.content.decode()
        self.assertNotIn('__all__', content)
        self.assertEqual(
            content.count('Please enter a correct username and password'), 1
        )


class EmployeeRoleGroupSyncTests(TestCase):
    """The `role` field is just a label -- the Groups created in signals.py
    are what actually grant permissions. These tests make sure the two stay
    in sync, since that's the whole point of the accounts app."""

    def test_new_employee_is_added_to_matching_group(self):
        employee = Employee.objects.create_user(
            username='cashier1', password='pass12345', role=Employee.CASHIER
        )
        self.assertEqual(
            list(employee.groups.values_list('name', flat=True)), ['Cashiers']
        )

    def test_role_change_moves_employee_to_new_group(self):
        employee = Employee.objects.create_user(
            username='warehouse1', password='pass12345', role=Employee.WAREHOUSE
        )
        employee.role = Employee.MANAGER
        employee.save()
        self.assertEqual(
            list(employee.groups.values_list('name', flat=True)), ['Managers']
        )


class EmployeePermissionTests(TestCase):
    """A Cashier or Warehouse employee must not be able to add/edit/delete
    other employees -- that used to be possible with just LoginRequiredMixin,
    which let anyone (including a Cashier editing themselves) grant their own
    account the Manager role."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager1', password='pass12345', role=Employee.MANAGER
        )
        self.cashier = Employee.objects.create_user(
            username='cashier1', password='pass12345', role=Employee.CASHIER
        )

    def test_cashier_cannot_reach_register(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_reach_register(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 200)

    def test_cashier_cannot_edit_employee(self):
        self.client.force_login(self.cashier)
        response = self.client.get(
            reverse('employee_edit', kwargs={'pk': self.cashier.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_cashier_cannot_delete_employee(self):
        self.client.force_login(self.cashier)
        response = self.client.get(
            reverse('employee_delete', kwargs={'pk': self.manager.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_manager_can_edit_employee(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse('employee_edit', kwargs={'pk': self.cashier.pk})
        )
        self.assertEqual(response.status_code, 200)
