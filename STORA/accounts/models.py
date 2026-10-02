from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.indexes import GinIndex
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

class Employee(AbstractUser):
    MANAGER = 'Manager'
    CASHIER = 'Cashier'
    WAREHOUSE = 'Warehouse'
    ROLE_CHOOSER = [(MANAGER, _('Manager')), (CASHIER, _('Cashier')), (WAREHOUSE, _('Warehouse'))]

    phone = models.CharField(max_length=20, blank=True, verbose_name=_('Phone number'))
    role = models.CharField(max_length=9, choices=ROLE_CHOOSER, default=CASHIER)

    class Meta:
        indexes = [
            GinIndex(fields=['first_name'], name='employee_first_name_trgm', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['last_name'], name='employee_last_name_trgm', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['username'], name='employee_username_trgm', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f'{self.first_name} {self.last_name} ({self.role})'


class CompanyProfile(models.Model):
    """The shop's own details -- name/BULSTAT/VAT/address -- for printing on
    outgoing documents (currently just the Orders "заявка" blank, see
    STORA.orders). Deliberately a single row, not one row per "site": this
    is a one-store pilot, and a real multi-site setup would need a bigger
    redesign anyway (per-site stock, etc.), not just more rows here.
    get_solo() below is the only supported way to fetch/create it -- there's
    no list/create UI, just one edit screen (see CompanyProfileUpdateView)."""

    name = models.CharField(max_length=120, verbose_name=_('Company name'))
    bulstat = models.CharField(
        max_length=12, blank=True,
        validators=[RegexValidator(regex=r'^\d+$', message=_('BULSTAT must contain only digits'), code='invalid_bulstat')],
        verbose_name=_('BULSTAT'),
    )
    # Optional, same pattern as Suppliers.vat_n -- not every shop is VAT
    # registered.
    vat_n = models.CharField(
        blank=True, max_length=14,
        validators=[RegexValidator(regex=r'^BG\d+$', message=_('VAT number must start with BG followed by digits'), code='invalid_vat')],
        verbose_name=_('VAT number'),
    )
    address = models.CharField(max_length=200, blank=True, verbose_name=_('Site address'))

    class Meta:
        verbose_name = _('Company Profile')
        verbose_name_plural = _('Company Profile')

    def __str__(self):
        return self.name or 'Company Profile'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


