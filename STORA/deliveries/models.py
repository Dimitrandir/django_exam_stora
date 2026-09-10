from decimal import Decimal

from django.db import models, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.core.validators import MinValueValidator
from django.utils import timezone

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


class DeliveryAttributes(models.Model):
    receiver = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='deliveries',
                                verbose_name='Receiver')
    supplier = models.ForeignKey(Suppliers, on_delete=models.PROTECT, related_name='deliveries',
                                verbose_name='Supplier')
    time_of_delivery = models.DateTimeField(default=timezone.now, verbose_name='Delivery Time')
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name='deliveries',
                                      verbose_name='Document Type')

    document_number = models.CharField(max_length=10, blank=True, null=True, verbose_name='Document Number')
    document_date = models.DateField(verbose_name='Document Date')
    total_amount = models.DecimalField(default=0.00, decimal_places=2, max_digits=12)

    class Meta:
        verbose_name = 'Delivery'
        verbose_name_plural = 'Deliveries'

    def __str__(self):
        return f"{self.receiver} {self.time_of_delivery}"

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

            if not self.price_at_delivery:
                self.price_at_delivery = product.delivery_price
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

            product.quantity += delta
            product.save(update_fields=['quantity'])

            super().save(*args, **kwargs)


@receiver(post_save, sender=DeliveryItems)
def _update_delivery_total(sender, instance, **kwargs):
    instance.delivery.recalculate_total()


@receiver(post_delete, sender=DeliveryItems)
def _restore_stock_on_delete(sender, instance, **kwargs):
    # If a delivery item is removed (mistake on the receiver's part, or the
    # whole delivery gets deleted -- CASCADE triggers this per item), take
    # back the stock it added and refresh the parent delivery's total.
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=instance.delivery_item_id)
        product.quantity -= instance.delivery_quantity
        product.save(update_fields=['quantity'])
    try:
        instance.delivery.recalculate_total()
    except DeliveryAttributes.DoesNotExist:
        pass  # whole delivery was deleted -- nothing left to recalculate
