from decimal import Decimal

from django.db import models, transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.validators import MinValueValidator


from STORA.accounts.models import Employee
from STORA.products.models import Product, Category

class SaleAttributes(models.Model):
    CASH = 'CASH'
    CARD = 'CARD'
    MIXED = 'MIXED'
    PAYMENT_METHOD_CHOICES = [(CASH, 'Cash'), (CARD, 'Card'), (MIXED, 'Mixed')]

    cashier = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='sales',
                                verbose_name='Cashier')
    time_of_sale = models.DateTimeField(auto_now_add=True, verbose_name='Sale Time')
    total_amount = models.DecimalField(default=0.00, decimal_places=2, max_digits=12)
    # Nullable -- existing rows predate the checkout screen (Фаза 2) that
    # sets these. New sales always get one via the checkout modal's own
    # client-side requirement, not a DB-level NOT NULL, so nothing here
    # breaks mid-rollout while that screen is still being built in stages.
    payment_method = models.CharField(max_length=5, choices=PAYMENT_METHOD_CHOICES, null=True, blank=True,
                                      verbose_name='Payment Method')
    # `amount_paid` is always the CASH portion (gross cash handed over --
    # for CASH it can exceed the total, giving change; for MIXED it's just
    # the cash remainder after `card_amount`, paid exactly, no change; for
    # CARD it's 0). `card_amount` is the portion charged to card (0 for
    # CASH, the full total for CARD, a partial amount for MIXED). Storing
    # both this way means `card_amount + amount_paid - change_due ==
    # total_amount` always holds, regardless of method -- no branching by
    # payment_method needed in a later report.
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                      verbose_name='Amount Paid (Cash)')
    card_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                      verbose_name='Amount Paid (Card)')
    change_due = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     verbose_name='Change Due')

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


class PosPin(models.Model):
    """One button in the fixed top bar of the POS category panel -- either
    a category-folder shortcut or a single-product shortcut, in a cashier-
    chosen order. Exactly one of category/product is set (DB constraint
    below); "edit mode" on the POS screen (sale_add.html) only offers
    items with `show_on_pos=True` to pin, but this model doesn't enforce
    that itself -- it's a picker-time rule, not a data invariant (unpinning
    is always allowed even if show_on_pos was later turned off)."""
    category = models.ForeignKey(Category, on_delete=models.CASCADE, null=True, blank=True, related_name='pos_pins')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True, related_name='pos_pins')
    position = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'POS Pin'
        verbose_name_plural = 'POS Pins'
        ordering = ['position']
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(category__isnull=False, product__isnull=True)
                    | models.Q(category__isnull=True, product__isnull=False)
                ),
                name='pospin_exactly_one_of_category_or_product',
            ),
        ]

    def __str__(self):
        target = self.category or self.product
        return f"Pin #{self.position}: {target}"


class SaleItemVoidLog(models.Model):
    """Audit trail for cart lines removed BEFORE a sale is ever completed --
    scanned an item, then took it back off (cashier mistake, customer
    changed their mind, or worth a manager's attention). This is not tied
    to a SaleAttributes row: the cart these lines came from is still just
    session draft state at removal time, may never turn into a saved sale
    at all, and there's no requirement it must. Written from sale_add.html
    via the dedicated log_removed_sale_item endpoint -- there is no review
    screen for this yet (deferred as a separate task), only the raw log."""

    REMOVE_LINE = 'REMOVE_LINE'
    VOID_SALE = 'VOID_SALE'
    ACTION_CHOICES = [(REMOVE_LINE, 'Removed one line'), (VOID_SALE, 'Voided whole sale')]

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='sale_item_voids')
    # SET_NULL, not PROTECT/CASCADE -- this is a historical log entry; a
    # product being deleted later shouldn't be blocked by, or take down,
    # an old audit row about it.
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, related_name='sale_item_voids')
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_price = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    total_price = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    action = models.CharField(max_length=15, choices=ACTION_CHOICES)
    removed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Removed Sale Item'
        verbose_name_plural = 'Removed Sale Items'
        ordering = ['-removed_at']

    def __str__(self):
        return f"{self.employee} {self.get_action_display()}: {self.quantity} x {self.product}"


class RefundAttributes(models.Model):
    """A refund (сторно) against an already-completed sale -- may cover the
    whole sale or just some of its lines/quantities (see RefundItems). Kept
    as its own model pair rather than reusing SaleAttributes/SaleItems with
    a movement-type flag (the Stock Movements pattern in `deliveries`)
    because a refund fundamentally references a specific original sale and
    must track how much of each of its lines has already been refunded, to
    stop the same line being refunded twice over -- bookkeeping that a bare
    direction flag on the same table doesn't capture.

    The 3 reasons mirror the fixed `Reason` values the fiscal protocol
    (Daisy ECRCommApp, FDStartFiscRcp) expects for a storno document, so a
    later fiscal-printer integration can pass this straight through without
    a translation table."""

    RETURN_COMPLAINT = 'RETURN_COMPLAINT'
    OPERATOR_ERROR = 'OPERATOR_ERROR'
    TAX_BASE_REDUCTION = 'TAX_BASE_REDUCTION'
    REASON_CHOICES = [
        (RETURN_COMPLAINT, 'Return / Complaint'),
        (OPERATOR_ERROR, 'Operator Error'),
        (TAX_BASE_REDUCTION, 'Reduction of Tax Base'),
    ]

    # PROTECT, not CASCADE -- a refund is a record of money actually handed
    # back; it must never silently vanish because the original sale row did.
    original_sale = models.ForeignKey(SaleAttributes, on_delete=models.PROTECT, related_name='refunds')
    cashier = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='refunds')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    time_of_refund = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(default=0.00, decimal_places=2, max_digits=12)

    class Meta:
        verbose_name = 'Refund'
        verbose_name_plural = 'Refunds'
        ordering = ['-time_of_refund']

    def __str__(self):
        return f"Refund #{self.pk} of Sale #{self.original_sale_id}"

    def recalculate_total(self):
        total = self.items.aggregate(total=models.Sum('total_price_row'))['total'] or Decimal('0.00')
        RefundAttributes.objects.filter(pk=self.pk).update(total_amount=total)
        self.total_amount = total


class RefundItems(models.Model):
    # PROTECT -- same reasoning as original_sale above; also lets
    # `refund_items` be summed against the original line (see refund_new
    # view) to cap how much of it can still be refunded.
    refund = models.ForeignKey(RefundAttributes, on_delete=models.CASCADE, related_name='items')
    original_item = models.ForeignKey(SaleItems, on_delete=models.PROTECT, related_name='refund_items')
    refund_quantity = models.DecimalField(max_digits=10, decimal_places=3,
                                          validators=[MinValueValidator(Decimal('0.001'))])
    price_at_refund = models.DecimalField(max_digits=9, decimal_places=2)
    total_price_row = models.DecimalField(max_digits=9, decimal_places=2, blank=True, null=True)

    class Meta:
        verbose_name = 'Refund Item'
        verbose_name_plural = 'Refund Items'

    def __str__(self):
        return f"{self.original_item.sale_item.name} x{self.refund_quantity}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            # Gives stock back -- mirrors adjust_stock_for_sale with a
            # negative delta (a refund is "un-selling"), including the
            # recipe-ingredient case.
            product = Product.objects.select_for_update().get(pk=self.original_item.sale_item_id)
            self.total_price_row = self.refund_quantity * self.price_at_refund
            adjust_stock_for_sale(product, -self.refund_quantity)
            super().save(*args, **kwargs)


@receiver(post_save, sender=RefundItems)
def _update_refund_total(sender, instance, **kwargs):
    instance.refund.recalculate_total()