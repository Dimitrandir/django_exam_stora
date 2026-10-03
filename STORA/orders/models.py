from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from STORA.accounts.models import Employee
from STORA.products.models import Product, Suppliers


class OrderCounter(models.Model):
    """Single-row counter backing OrderAttributes.order_number -- plain
    sequential digits starting at 1 (asked for live: "номера ... да е само
    цифри ... и да започва от 1"), not date-based like the write-off/scrap
    document numbers. Same locking pattern as sales.FiscalCounter, so two
    orders saved at the same moment can never draw the same number."""

    next_number = models.PositiveIntegerField(default=1)

    @classmethod
    def next(cls):
        with transaction.atomic():
            counter, _created = cls.objects.select_for_update().get_or_create(pk=1)
            n = counter.next_number
            counter.next_number = n + 1
            counter.save(update_fields=['next_number'])
            return n


class OrderAttributes(models.Model):
    """One "заявка" -- a restock request drafted for a single supplier,
    covering a chosen sales period (see OrderItems for the actual lines).
    Mirrors the SaleAttributes/DeliveryAttributes "header + line items"
    pattern used everywhere else in the project."""

    POSITION_PRIMARY = 1
    POSITION_SECONDARY = 2
    POSITION_TERTIARY = 3
    POSITION_CHOICES = [
        (POSITION_PRIMARY, _('Primary (1st)')),
        (POSITION_SECONDARY, _('Secondary (2nd)')),
        (POSITION_TERTIARY, _('Tertiary (3rd)')),
    ]

    STATUS_DRAFT = 'DRAFT'
    STATUS_CONFIRMED = 'CONFIRMED'
    STATUS_CHOICES = [
        (STATUS_DRAFT, _('Draft (AI)')),
        (STATUS_CONFIRMED, _('Confirmed')),
    ]

    supplier = models.ForeignKey(Suppliers, on_delete=models.PROTECT, related_name='orders', verbose_name=_('Supplier'))
    # Every order built by hand on the compose screen (order_new) is
    # CONFIRMED the moment it's saved -- a human picked the supplier/period
    # and reviewed the lines before hitting Save, same as always. DRAFT only
    # ever comes from "AI Draft All" (order_ai_draft_all): Claude decided
    # both which products and how much on its own, so those orders sit here
    # for a human to open, adjust if needed, and explicitly Confirm (or
    # Discard) before they're treated as real -- see order_confirm/
    # order_discard.
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_CONFIRMED, verbose_name=_('Status'))
    # Which of the product's ranked suppliers (ProductSupplier.position)
    # the candidate list was built from -- comma-joined ("1,2"), not a
    # single choices field any more, since the compose screen now lets you
    # tick more than one position at once (checkboxes, asked for live so
    # "да можеш да избираш и трите едновременно"). get_supplier_position_display()
    # is hand-written below to replace the auto one Django only generates
    # for an actual `choices=` field.
    supplier_position = models.CharField(max_length=20, blank=True, verbose_name=_('Supplier position(s)'))
    period_start = models.DateField(verbose_name=_('Sales period start'))
    period_end = models.DateField(verbose_name=_('Sales period end'))
    # Auto-generated on first save when left blank -- see OrderCounter.
    order_number = models.CharField(max_length=10, blank=True, verbose_name=_('Order Number'))
    order_date = models.DateField(default=timezone.localdate, verbose_name=_('Order Date'))
    created_by = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='orders', verbose_name=_('Created by'))

    class Meta:
        verbose_name = _('Order')
        verbose_name_plural = _('Orders')
        ordering = ['-order_date', '-pk']

    def __str__(self):
        return f'{self.order_number or f"Order #{self.pk}"} -- {self.supplier}'

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = str(OrderCounter.next())
        super().save(*args, **kwargs)

    def get_position_list(self):
        """[1, 2] from stored "1,2" -- what the compose/edit screens and
        the candidate-product query actually work with."""
        return [int(p) for p in self.supplier_position.split(',') if p]

    def set_position_list(self, positions):
        self.supplier_position = ','.join(str(p) for p in sorted(set(positions)))

    def get_supplier_position_display(self):
        labels = dict(self.POSITION_CHOICES)
        return ', '.join(str(labels.get(p, p)) for p in self.get_position_list())


class OrderItems(models.Model):
    """One requested line on an order -- product.name/barcode and this
    quantity are the only three things that make it onto the printed
    blank; sales-for-period/distinct-sale-count are computed live from
    SaleItems when a screen needs them, not stored here (the period is
    already on OrderAttributes, so there's nothing to snapshot against
    going stale)."""

    order = models.ForeignKey(OrderAttributes, on_delete=models.CASCADE, related_name='items', verbose_name=_('Order'))
    # PROTECT, not CASCADE -- same reasoning as SaleItems/DeliveryItems: a
    # product that's been ordered before shouldn't be hard-deletable out
    # from under this history (ProductDeleteView already offers "Archive
    # instead" on any ProtectedError, no extra handling needed here).
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='order_items', verbose_name=_('Product'))
    requested_quantity = models.DecimalField(
        max_digits=10, decimal_places=3, validators=[MinValueValidator(Decimal('0.001'))],
        verbose_name=_('Requested quantity'),
    )

    class Meta:
        verbose_name = _('Order Item')
        verbose_name_plural = _('Order Items')
        constraints = [
            models.UniqueConstraint(fields=['order', 'product'], name='unique_order_product'),
        ]

    def __str__(self):
        return f'{self.product} x{self.requested_quantity} ({self.order})'
