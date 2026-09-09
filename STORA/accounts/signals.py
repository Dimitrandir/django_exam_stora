from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver

from STORA.accounts.models import Employee


@receiver(post_migrate)
def create_default_groups(sender, **kwargs):
    if sender.name not in {'STORA.accounts', 'STORA.products', 'STORA.sales'}:
        return

    managers_group, _ = Group.objects.get_or_create(name='Managers')
    cashiers_group, _ = Group.objects.get_or_create(name='Cashiers')
    warehouse_group, _ = Group.objects.get_or_create(name='Warehouse')

    managers_group.permissions.set(Permission.objects.all())

    sale_permissions = Permission.objects.filter(
        content_type__app_label='sales'
    )
    # Cashiers also need read-only access to products/categories/suppliers to
    # look up prices and stock while ringing up a sale -- editing stays
    # Manager/Warehouse only.
    cashier_view_permissions = Permission.objects.filter(
        content_type__app_label='products',
        codename__in=['view_product', 'view_category', 'view_suppliers'],
    )
    cashiers_group.permissions.set(list(sale_permissions) + list(cashier_view_permissions))

    product_permissions = Permission.objects.filter(
        content_type__app_label='products',
        codename__in=[
            'view_product',
            'add_product',
            'change_product',
            'view_category',
            'add_category',
            'change_category',
            'view_suppliers',
            'add_suppliers',
            'change_suppliers',
            'view_taxgroup',
            'add_taxgroup',
            'change_taxgroup',
        ]
    )
    warehouse_group.permissions.set(product_permissions)


ROLE_TO_GROUP = {
    Employee.MANAGER: 'Managers',
    Employee.CASHIER: 'Cashiers',
    Employee.WAREHOUSE: 'Warehouse',
}


@receiver(post_save, sender=Employee)
def sync_employee_role_group(sender, instance, **kwargs):
    # Keeps Django's actual permission groups in step with the human-readable
    # `role` field, so changing role in the edit form immediately changes
    # what the employee is allowed to do -- not just what label is shown.
    group_name = ROLE_TO_GROUP.get(instance.role)
    if not group_name:
        return
    group, _ = Group.objects.get_or_create(name=group_name)
    instance.groups.set([group])