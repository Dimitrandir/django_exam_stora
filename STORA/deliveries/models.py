from decimal import Decimal

from django.db import models, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.utils.dateparse import parse_date

from STORA.accounts.models import Employee
from STORA.products.models import Product, Suppliers


class DocumentType(models.Model):
    """What kind of paperwork a delivery arrived with (invoice, delivery
    note, ...) -- a manageable list like Category/TaxGroup, not a hardcoded
    set, so a Manager can add a new type from the UI without a code change."""

    name = models.CharField(max_length=60, unique=True)

    class Meta:
        verbose_name = 'Document Type'
        verbose_name_plural = 'Document Types'
        ordering = ['name']

    def __str__(self):
        return self.name


class ScrapReason(models.Model):
    """Why a batch got scrapped (expired, damaged, ...) -- a manageable list
    like DocumentType, not hardcoded choices, since the set of reasons is a
    business/labeling concern, not something that changes DeliveryItems.save()
    behavior the way movement_type does."""

    name = models.CharField(max_length=60, unique=True)

    class Meta:
        verbose_name = 'Scrap Reason'
        verbose_name_plural = 'Scrap Reasons'
        ordering = ['name']

    def __str__(self):
        return self.name


class DeliveryAttributes(models.Model):
    # Fixed business categories, not a manageable list like DocumentType --
    # the value directly drives which direction DeliveryItems.save() moves
    # Product.quantity, so a new one needs code changes anyway.
    MOVEMENT_DELIVERY = 'DELIVERY'
    MOVEMENT_WRITE_OFF = 'WRITE_OFF'
    MOVEMENT_SCRAP = 'SCRAP'
    MOVEMENT_TYPE_CHOICES = [
        (MOVEMENT_DELIVERY, 'Delivery'),
        (MOVEMENT_WRITE_OFF, 'Write-off'),
        (MOVEMENT_SCRAP, 'Scrap'),
    ]
    # Both move stock OUT -- shared by DeliveryItems.save()'s sign check and
    # by templates deciding when to show the red/minus "outgoing" styling.
    OUTGOING_MOVEMENT_TYPES = (MOVEMENT_WRITE_OFF, MOVEMENT_SCRAP)

    movement_type = models.CharField(max_length=10, choices=MOVEMENT_TYPE_CHOICES,
                                     default=MOVEMENT_DELIVERY, verbose_name='Movement Type')
    receiver = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='deliveries',
                                verbose_name='Receiver')
    # Nullable -- scrap has no supplier (it's stock leaving because it went
    # bad/broke, not stock going back to whoever delivered it).
    supplier = models.ForeignKey(Suppliers, on_delete=models.PROTECT, related_name='deliveries',
                                verbose_name='Supplier', null=True, blank=True)
    time_of_delivery = models.DateTimeField(default=timezone.now, verbose_name='Delivery Time')
    # Nullable -- a write-off has no incoming invoice/delivery note, so
    # there's no meaningful DocumentType to pick for it.
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name='deliveries',
                                      verbose_name='Document Type', null=True, blank=True)

    # Widened from 10 -- write-offs auto-generate an internal number like
    # "WO-20260910-001" when left blank (see _generate_internal_document_number)
    # to tell apart same-day write-offs to the same supplier.
    document_number = models.CharField(max_length=20, blank=True, null=True, verbose_name='Document Number')
    document_date = models.DateField(verbose_name='Document Date')
    # Free-text note -- shared by all three movement types (Delivery/
    # Write-off/Scrap), same as every other field on this model. TextField,
    # not CharField, since a receiver/warehouse clerk may want more than a
    # short label (e.g. "3 crates damaged in transit, supplier notified").
    comment = models.TextField(blank=True, null=True, verbose_name='Comment')
    total_amount = models.DecimalField(default=0.00, decimal_places=2, max_digits=12)

    class Meta:
        verbose_name = 'Delivery'
        verbose_name_plural = 'Deliveries'

    def __str__(self):
        return f"{self.receiver} {self.time_of_delivery}"

    def save(self, *args, **kwargs):
        if self.movement_type != self.MOVEMENT_DELIVERY and not self.document_number:
            self.document_number = self._generate_internal_document_number()
        super().save(*args, **kwargs)

    def _generate_internal_document_number(self):
        prefix = 'WO' if self.movement_type == self.MOVEMENT_WRITE_OFF else 'SCRAP'
        doc_date = self.document_date or timezone.localdate()
        # A real form submission always hands this a proper `date` (DateField
        # runs it through to_python() first) -- this only guards direct
        # `.objects.create(document_date='2026-04-20')` calls (tests/scripts),
        # same caveat as the Decimal*float gotcha documented in CLAUDE.md.
        if isinstance(doc_date, str):
            doc_date = parse_date(doc_date) or timezone.localdate()
        same_day_count = DeliveryAttributes.objects.filter(
            movement_type=self.movement_type,
            document_date=doc_date,
        ).count()
        return f'{prefix}-{doc_date.strftime("%Y%m%d")}-{same_day_count + 1:03d}'

    def recalculate_total(self):
        """Recomputes total_amount straight from the DB (not from possibly
        stale in-memory items) and writes only that one field -- same
        pattern as SaleAttributes.recalculate_total()."""
        total = self.items.aggregate(total=models.Sum('total_price_row'))['total'] or Decimal('0.00')
        DeliveryAttributes.objects.filter(pk=self.pk).update(total_amount=total)
        self.total_amount = total


class DeliveryItems(models.Model):
    delivery = models.ForeignKey(DeliveryAttributes, on_delete=models.CASCADE, related_name='items',
                             verbose_name='Delivery Reference')
    delivery_item = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='delivered_items',
                                  verbose_name='Product')
    # Decimal, not an integer count -- matches Product.quantity /
    # SaleItems.sale_quantity, so `unit_type=weight` products can be
    # received in fractional amounts (e.g. 12.500 kg).
    delivery_quantity = models.DecimalField(default=1, max_digits=10, decimal_places=3,
                                            validators=[MinValueValidator(Decimal('0.001'))],
                                            verbose_name='Quantity Delivered')
    price_at_delivery = models.DecimalField(blank=True, null= True, max_digits=9, decimal_places=2,
                                        verbose_name='Unit Price at Delivery',
                                        help_text='Price of the product at the moment of delivery')
    total_price_row = models.DecimalField(blank=True, null= True, max_digits=9, decimal_places=2,
                                        verbose_name='Total price',
                                        help_text='Total price of the current article')
    # Per delivery line, not per product -- the same product can arrive in
    # different batches with different expiry dates.
    expiry_date = models.DateField(blank=True, null=True, verbose_name='Expiry Date')
    # Scrap variant 1 (picking a specific delivered batch to scrap) points
    # this back at the original DELIVERY row for traceability -- variant 2
    # (free entry: product + quantity + reason, no batch reference, used to
    # scrap something before its tracked expiry) leaves it blank. SET_NULL
    # so deleting the original delivery item doesn't cascade-delete scrap
    # history that referenced it.
    source_item = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='scrapped_as', verbose_name='Source Batch')
    # Only meaningful when the parent delivery's movement_type is SCRAP.
    scrap_reason = models.ForeignKey(ScrapReason, on_delete=models.PROTECT, null=True, blank=True,
                                     related_name='scrap_items', verbose_name='Scrap Reason')

    class Meta:
        verbose_name = 'Delivery Item'
        verbose_name_plural = 'Delivery Items'

    def __str__(self):
        return f"{self.delivery_item_id} {self.delivery_item.name} {self.delivery_quantity}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            # select_for_update locks this product row until the transaction
            # commits, so two simultaneous deliveries of the same product
            # can't both read the same "before" quantity and silently
            # overwrite each other (lost update) -- same reasoning as
            # SaleItems.save().
            product = Product.objects.select_for_update().get(pk=self.delivery_item_id)

            if self.price_at_delivery is None:
                self.price_at_delivery = product.delivery_price
            if self.price_at_delivery is None:
                # Product has no delivery_price in the catalog yet either
                # (e.g. brand-new product) -- fall back to 0 instead of
                # crashing on Decimal * None. Matches what the delivery
                # items grid already shows the clerk in that case (see
                # _delivery_items_table.html, addProductRow).
                self.price_at_delivery = Decimal('0.00')
            self.total_price_row = self.delivery_quantity * self.price_at_delivery

            if self.pk:
                # Editing an existing delivery item: apply only the CHANGE
                # in quantity, not the new quantity added a second time on
                # top of what the first save already added.
                old_quantity = DeliveryItems.objects.get(pk=self.pk).delivery_quantity
                delta = self.delivery_quantity - old_quantity
            else:
                # Brand new item: add the full quantity once.
                delta = self.delivery_quantity

            # The clerk always types a positive quantity, whether receiving
            # stock or writing it off -- the sign of the effect on
            # Product.quantity is decided here, from the parent's
            # movement_type, not by asking for a negative number in the UI.
            sign = -1 if self.delivery.movement_type in DeliveryAttributes.OUTGOING_MOVEMENT_TYPES else 1
            product.quantity += sign * delta
            product.save(update_fields=['quantity'])

            super().save(*args, **kwargs)


@receiver(post_save, sender=DeliveryItems)
def _update_delivery_total(sender, instance, **kwargs):
    instance.delivery.recalculate_total()


@receiver(post_delete, sender=DeliveryItems)
def _restore_stock_on_delete(sender, instance, **kwargs):
    # If a delivery item is removed (mistake on the receiver's part, or the
    # whole delivery gets deleted -- CASCADE triggers this per item), undo
    # whatever effect it had on stock and refresh the parent delivery's
    # total. Falls back to the DELIVERY (add) sign if the parent is already
    # gone -- matches the pre-write-off behavior for that edge case.
    try:
        movement_type = instance.delivery.movement_type
    except DeliveryAttributes.DoesNotExist:
        movement_type = DeliveryAttributes.MOVEMENT_DELIVERY
    sign = -1 if movement_type in DeliveryAttributes.OUTGOING_MOVEMENT_TYPES else 1

    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=instance.delivery_item_id)
        product.quantity -= sign * instance.delivery_quantity
        product.save(update_fields=['quantity'])
    try:
        instance.delivery.recalculate_total()
    except DeliveryAttributes.DoesNotExist:
        pass  # whole delivery was deleted -- nothing left to recalculate
