from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.indexes import GinIndex
from django.db import models

class Employee(AbstractUser):
    MANAGER = 'Manager'
    CASHIER = 'Cashier'
    WAREHOUSE = 'Warehouse'
    ROLE_CHOOSER = [(MANAGER, 'Manager'), (CASHIER, 'Cashier'),(WAREHOUSE, 'Warehouse')]

    phone = models.CharField(max_length=20, blank=True, verbose_name='Phone number')
    role = models.CharField(max_length=9, choices=ROLE_CHOOSER, default=CASHIER)

    class Meta:
        indexes = [
            GinIndex(fields=['first_name'], name='employee_first_name_trgm', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['last_name'], name='employee_last_name_trgm', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['username'], name='employee_username_trgm', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f'{self.first_name} {self.last_name} ({self.role})'


