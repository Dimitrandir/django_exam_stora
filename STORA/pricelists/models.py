from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from STORA.accounts.models import Employee
from STORA.products.models import Category, Product, Suppliers


class PriceList(models.Model):
    name = models.CharField(max_length=100, unique=True)
    # Decides which price list wins when a product is covered by more than
    # one currently-active list at once (e.g. a personal promo AND a
    # brochure both touching the same product) -- higher wins. See
    # resolve_prices() below for the full tie-break order.
    priority = models.PositiveIntegerField(default=0, verbose_name='Priority')
    start_date = models.DateField(verbose_name='Start Date')
    end_date = models.DateField(verbose_name='End Date')
    # Soft delete -- keeps the list (and its past effect, visible on
    # anything that already referenced it) instead of losing history, same
    # convention as everywhere else in the app that avoids hard deletes.
    is_deleted = models.BooleanField(default=False)
    created_by = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name='price_lists_created',
                                   verbose_name='Created By')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Price List'
        verbose_name_plural = 'Price Lists'
        ordering = ['-priority', '-created_at']

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError('End date cannot be before start date.')

    def is_in_period(self, on_date=None):
        on_date = on_date or timezone.localdate()
        return self.start_date <= on_date <= self.end_date

    def __str__(self):
        return self.name


class PriceListRule(models.Model):
    """One line of a PriceList -- covers either a single product, a whole
    category, or a whole supplier's products (never more than one of the
    three at once). A list built "mixed" (some individual articles, some
    whole categories/suppliers) is just several rules of different
    scope_type under the same PriceList.

    Category/Supplier rules are deliberately DYNAMIC, not a frozen snapshot
    of whatever products matched at creation time -- a product added to a
    covered category/supplier later automatically picks up the discount,
    and the discount is computed from the product's CURRENT sell_price at
    the moment of resolution (see resolve_prices()), not a price frozen
    when the rule was created. That also means only a percentage discount
    makes sense for those two scopes (a flat new price can't apply
    uniformly across products that started at different prices) --
    fixed_price is only ever set for scope_type=PRODUCT.
    """

    SCOPE_PRODUCT = 'PRODUCT'
    SCOPE_CATEGORY = 'CATEGORY'
    SCOPE_SUPPLIER = 'SUPPLIER'
    SCOPE_CHOICES = [(SCOPE_PRODUCT, 'Product'), (SCOPE_CATEGORY, 'Category'), (SCOPE_SUPPLIER, 'Supplier')]
    # Lower number = more specific -- the tie-break order within a single
    # PriceList (or across lists sharing the same priority) when a product
    # is somehow reachable through more than one rule at once.
    SCOPE_SPECIFICITY = {SCOPE_PRODUCT: 0, SCOPE_CATEGORY: 1, SCOPE_SUPPLIER: 2}

    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name='rules')
    scope_type = models.CharField(max_length=10, choices=SCOPE_CHOICES, verbose_name='Applies To')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='price_list_rules')
    category = models.ForeignKey(Category, on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='price_list_rules')
    supplier = models.ForeignKey(Suppliers, on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='price_list_rules')
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        verbose_name='Discount %',
    )
    # PRODUCT scope only -- see class docstring.
    fixed_price = models.DecimalField(
        max_digits=9, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.01'))], verbose_name='Fixed Price',
    )

    class Meta:
        verbose_name = 'Price List Rule'
        verbose_name_plural = 'Price List Rules'

    def clean(self):
        scope_fields = {
            self.SCOPE_PRODUCT: 'product',
            self.SCOPE_CATEGORY: 'category',
            self.SCOPE_SUPPLIER: 'supplier',
        }
        expected_field = scope_fields.get(self.scope_type)
        if expected_field is None:
            raise ValidationError('Choose what this rule applies to.')
        for scope, field_name in scope_fields.items():
            value = getattr(self, f'{field_name}_id')
            if field_name == expected_field and not value:
                raise ValidationError(f'A {self.scope_type.lower()}-scoped rule needs a {field_name}.')
            if field_name != expected_field and value:
                raise ValidationError(f'A {self.scope_type.lower()}-scoped rule cannot also set {field_name}.')

        if self.fixed_price is not None and self.scope_type != self.SCOPE_PRODUCT:
            raise ValidationError('A fixed price can only be set on a product-scoped rule -- '
                                   'category/supplier rules can only use a percentage discount.')
        if self.discount_percent is None and self.fixed_price is None:
            raise ValidationError('Set either a discount percentage or a fixed price.')
        if self.discount_percent is not None and self.fixed_price is not None:
            raise ValidationError('Set either a discount percentage or a fixed price, not both.')

    def effective_price(self, base_price):
        """The price this rule produces for a product whose regular price
        is `base_price` (a Decimal)."""
        if self.fixed_price is not None:
            return self.fixed_price
        multiplier = (Decimal('100') - self.discount_percent) / Decimal('100')
        return (base_price * multiplier).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    def __str__(self):
        target = {
            self.SCOPE_PRODUCT: self.product,
            self.SCOPE_CATEGORY: self.category,
            self.SCOPE_SUPPLIER: self.supplier,
        }.get(self.scope_type)
        return f'{self.price_list} -- {self.scope_type}: {target}'
