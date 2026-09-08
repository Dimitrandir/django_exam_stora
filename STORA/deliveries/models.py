from decimal import Decimal

from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone

from STORA.accounts.models import Employee
from STORA.products.models import Product, Suppliers


class DeliveryAttributes(models.Model):
    receiver = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='deliveries',
                                verbose_name='Receiver')
    supplier = models.ForeignKey(Suppliers, on_delete=models.PROTECT, related_name='deliveries',
                                verbose_name='Supplier')
    time_of_delivery = models.DateTimeField(default=timezone.now, verbose_name='Delivery Time')
    document_type = models.CharField(choices=[('INVOICE', 'Invoice'),
                                     ('DELIVERY_NOTE', 'Delivery Note')],
                                     verbose_name='Document Type')

    document_number = models.CharField(max_length=10, blank=True, null=True, verbose_name='Document Number')
    document_date = models.DateField(verbose_name='Document Date')
    total_amount = models.DecimalField(default=0.00, decimal_places=2, max_digits=12)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        total_sum = 0
        for item in self.items.all():
           total_sum += item.total_price_row or 0
        self.total_amount = total_sum
        super().save(update_fields=['total_amount'])


    class Meta:
        verbose_name = 'Delivery'
        verbose_name_plural = 'Deliveries'

    def __str__(self):
        return f"{self.receiver} {self.time_of_delivery}"

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

    def save(self, *args, **kwargs):
        if not self.price_at_delivery:
            self.price_at_delivery = self.delivery_item.delivery_price
        tpr = self.delivery_quantity * self.price_at_delivery
        self.total_price_row = tpr
        self.delivery_item.quantity += self.delivery_quantity
        self.delivery_item.save()
        super().save(*args, **kwargs)


    class Meta:
        verbose_name = 'Delivery Item'
        verbose_name_plural = 'Delivery Items'

    def __str__(self):
        return f"{self.delivery_item_id} {self.delivery_item.name} {self.delivery_quantity}"


