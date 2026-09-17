from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from STORA.accounts.models import Employee
from STORA.products.models import Product


class RevisionAttributes(models.Model):
    STATUS_OPEN = 'OPEN'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_CHOICES = [(STATUS_OPEN, 'Open'), (STATUS_COMPLETED, 'Completed'), (STATUS_CANCELLED, 'Cancelled')]

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN)
    started_by = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='revisions_started',
                                    verbose_name='Started By')
    started_at = models.DateTimeField(auto_now_add=True)
    # Reused for whichever way the revision was actually closed -- completed
    # (stock corrected) or cancelled (discarded, stock untouched). Templates
    # branch on `status` to label these "Completed by"/"Cancelled by"
    # appropriately; splitting into separate cancelled_by/cancelled_at
    # fields would just duplicate the same "who closed it, when" concept.
    completed_by = models.ForeignKey(Employee, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name='revisions_completed', verbose_name='Completed By')
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Stock Revision'
        verbose_name_plural = 'Stock Revisions'
        ordering = ['-started_at']
        constraints = [
            # DB-level guarantee that at most one revision is OPEN at a
            # time -- a partial unique index on `status`, only enforced for
            # rows where status='OPEN'. This is what actually protects
            # against two people clicking "Start Revision" at the exact
            # same moment (an application-level "is there one open?" check
            # followed by a create() has a race window; this doesn't).
            models.UniqueConstraint(fields=['status'], condition=Q(status='OPEN'), name='only_one_open_revision'),
        ]

    def __str__(self):
        return f'Revision #{self.pk} ({self.status})'


class RevisionItemsManager(models.Manager):
    def add_count(self, revision, product, quantity):
        """Adds `quantity` to this product's running found_quantity within
        the revision -- never overwrites it, so two people (on two
        computers) counting the same product both contribute to the same
        total instead of one silently clobbering the other's count (same
        idea as scanning a barcode twice in the cashier cart). Creates the
        row, snapshotting the product's current stock and price, the first
        time it's counted in this revision. select_for_update() on the
        get-or-create serializes two simultaneous counts of the same
        product so the increment can never be lost.
        """
        with transaction.atomic():
            item, created = self.select_for_update().get_or_create(
                revision=revision, product=product,
                defaults={
                    'system_quantity_at_start': product.quantity,
                    'price_at_revision': product.delivery_price,
                },
            )
            item.found_quantity += quantity
            item.save(update_fields=['found_quantity'])
            return item


class RevisionItems(models.Model):
    revision = models.ForeignKey(RevisionAttributes, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='revision_items')
    # Running total counted so far for this product in this revision --
    # accumulates across every scan/entry, possibly from several computers.
    found_quantity = models.DecimalField(default=Decimal('0'), max_digits=10, decimal_places=3,
                                          validators=[MinValueValidator(Decimal('0'))])
    # Snapshotted the first time this product is counted in this revision --
    # deliberately NOT a live lookup, so a sale rung up on another till
    # while the count is still in progress doesn't move the comparison
    # target out from under the counters.
    system_quantity_at_start = models.DecimalField(max_digits=10, decimal_places=3)
    price_at_revision = models.DecimalField(null=True, blank=True, max_digits=9, decimal_places=2)

    objects = RevisionItemsManager()

    class Meta:
        verbose_name = 'Revision Item'
        verbose_name_plural = 'Revision Items'
        constraints = [
            models.UniqueConstraint(fields=['revision', 'product'], name='one_row_per_product_per_revision'),
        ]

    def __str__(self):
        return f'{self.product_id} {self.found_quantity}'
