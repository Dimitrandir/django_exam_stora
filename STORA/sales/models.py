from decimal import Decimal

from django.db import models, transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.validators import MinValueValidator


from STORA.accounts.models import Employee
from STORA.products.models import Product

class SaleAttributes(models.Model):
    cashier = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='sales',
                                verbose_name='Cashier')
    time_of_sale = models.DateTimeField(auto_now_add=True, verbose_name='Sale Time')
    total_amount = models.DecimalField(default=0.00, decimal_places=2, max_digits=12)

    class Meta:
        verbose_name = 'Sale'
        verbose_name_plural = 'Sales'

    def __str__(self):
        return f"{self.cashier} {self.time_of_sale}"

    def recalculate_total(self):
        """Recomputes total_amount straight from the DB (not from possibly
        stale in-memory items) and writes only that one field."""
        total = self.items.aggregate(total=models.Sum('total_price_row'))['total'] or Decimal('0.00')
        SaleAttributes.objects.filter(pk=self.pk).update(total_amount=total)
        self.total_amount = total

class SaleItems(models.Model):
    sale = models.ForeignKey(SaleAttributes, on_delete=models.CASCADE, related_name='items',
                             verbose_name='Sale Reference')
    sale_item = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='sold_items',
                                  verbose_name='Product')
    # Decimal, not an integer count -- a `unit_type=weight` product can be
    # sold in fractional amounts (e.g. 0.350 kg); `piece` products just
    # always use a whole number in the same field.
    sale_quantity = models.DecimalField(default=1, max_digits=10, decimal_places=3,
                                        validators=[MinValueValidator(Decimal('0.001'))],
                                        verbose_name='Quantity Sold')
    price_at_sale = models.DecimalField(blank=True, null= True, max_digits=9, decimal_places=2,
                                        verbose_name='Unit Price at Sale',
                                        help_text='Price of the product at the moment of sale')
    total_price_row = models.DecimalField(blank=True, null= True, max_digits=9, decimal_places=2,
                                        verbose_name='Total price',
                                        help_text='Total price of the current article')

    class Meta:
        verbose_name = 'Sale Item'
        verbose_name_plural = 'Sales Items'

    def __str__(self):
        return f"{self.sale_item_id} {self.sale_item.name} {self.sale_quantity} {self.price_at_sale}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            # select_for_update locks this product row until the transaction
            # commits, so two simultaneous sales of the same product can't
            # both read the same "before" quantity and silently overwrite
            # each other (lost update). Negative stock is still allowed on
            # purpose (pre-delivery sales) -- we're only fixing correctness,
            # not blocking the minus.
            product = Product.objects.select_for_update().get(pk=self.sale_item_id)

            if not self.price_at_sale:
                self.price_at_sale = product.sell_price
            self.total_price_row = self.sale_quantity * self.price_at_sale

            if self.pk:
                # Editing an existing sale item: apply only the CHANGE in
                # quantity, not the new quantity a second time.
                old_quantity = SaleItems.objects.get(pk=self.pk).sale_quantity
                delta = self.sale_quantity - old_quantity
            else:
                # Brand new item: subtract the full quantity once.
                delta = self.sale_quantity

            adjust_stock_for_sale(product, delta)

            super().save(*args, **kwargs)


def adjust_stock_for_sale(product, delta):
    """Deducts `delta` units of `product` from stock. For a normal product
    that's just its own `quantity`; for a recipe product (`is_recipe=True`)
    it's each ingredient's quantity, scaled by `delta` -- selling 2
    cappuccinos deducts 2x the milk/coffee per recipe line, not the
    cappuccino's own (nonexistent) stock. Pass a negative `delta` to give
    stock back (e.g. deleting a sale item).

    Caller must already be inside `transaction.atomic()` -- this locks rows
    with `select_for_update()` but doesn't open its own transaction, so it
    can be called for a normal sale item's own product (already locked by
    the caller) without a redundant/nested lock attempt on the same row.
    """
    if not product.is_recipe:
        product.quantity -= delta
        product.save(update_fields=['quantity'])
        return

    ingredient_links = list(product.recipe_ingredients.select_related('ingredient'))
    if not ingredient_links:
        return
    ingredient_ids = [link.ingredient_id for link in ingredient_links]
    locked_ingredients = {
        ingredient.pk: ingredient
        for ingredient in Product.objects.select_for_update().filter(pk__in=ingredient_ids)
    }
    for link in ingredient_links:
        ingredient = locked_ingredients[link.ingredient_id]
        ingredient.quantity -= link.quantity * delta
        ingredient.save(update_fields=['quantity'])


@receiver(post_save, sender=SaleItems)
def _update_sale_total(sender, instance, **kwargs):
    instance.sale.recalculate_total()


@receiver(post_delete, sender=SaleItems)
def _restore_stock_on_delete(sender, instance, **kwargs):
    # If a sale item is removed (e.g. cashier made a mistake), give the
    # stock back (ingredients, if it was a recipe) and refresh the parent
    # sale's total.
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=instance.sale_item_id)
        adjust_stock_for_sale(product, -instance.sale_quantity)
    instance.sale.recalculate_total()