from decimal import Decimal

from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models



class Suppliers(models.Model):
    name = models.CharField(max_length=60, unique=True)
    bulstat = models.CharField(max_length=12,validators=[RegexValidator(regex='^\d+$',
                                                                          message='BULSTAT must contain only digits',
                                                                          code='invalid_bulstat')], unique=True,
                               verbose_name='BULSTAT')

    vat_n = models.CharField(blank= True, max_length=14, validators=[RegexValidator(regex='^BG\d+$',
                                                                          message='VAT number must start with BG followed by digits',
                                                                          code='invalid_vat')], verbose_name='VAT:',
                             help_text='Only if the company is VAT registered!')

    phone = models.CharField(max_length=20, blank=True, verbose_name='Phone number')
    email = models.EmailField(blank=True, verbose_name='Email address')

    class Meta:
        indexes = [
            GinIndex(fields=['name'], name='supplier_name_trgm_idx', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f'{self.name}'

class Category(models.Model):
    name = models.CharField(max_length=30, unique=True, null=True)
    description = models.CharField(max_length=120, blank=True, null=True)

    class Meta:
        indexes = [
            GinIndex(fields=['name'], name='category_name_trgm_idx', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f"{self.name}"

class Barcode(models.Model):
    code = models.CharField(max_length=13, null=True, blank=True, unique=True, verbose_name='Barcode number',
                            help_text='Scan the barcode or enter the EAN-13 number')
    position = models.PositiveSmallIntegerField(default=1, verbose_name='Position',
                                help_text='1 = primary barcode, 2+ = alternates')
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='barcode',
                                verbose_name='Linked Product')

    class Meta:
        verbose_name = "Barcode"
        verbose_name_plural = "Barcodes"
        ordering = ['position', 'id']

    def __str__(self):
       return f"{self.code}"


class Product(models.Model):
    PIECE = 'piece'
    WEIGHT = 'weight'
    UNIT_TYPE_CHOICES = [(PIECE, 'Piece (pcs)'), (WEIGHT, 'Weight (kg)')]

    internal_code = models.CharField(max_length=8, unique=True)
    name = models.CharField(max_length=90, unique=True, blank=False, null=False, verbose_name='product name')
    unit_type = models.CharField(max_length=6, choices=UNIT_TYPE_CHOICES, default=PIECE,
                                 verbose_name='sold by',
                                 help_text='Piece: whole units (bottles, packs). Weight: sold by the kg (produce, deli).')
    delivery_price = models.DecimalField(blank=True, null=True, max_digits=9, decimal_places=2,
                                         verbose_name='delivery price')
    sell_price = models.DecimalField(validators=[MinValueValidator(0.01)], max_digits=9,
                                     decimal_places=2, verbose_name='sale price', help_text="Selling price per unit")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name='product')
    # DecimalField (not Integer) so `unit_type=weight` products can carry
    # fractional stock like 2.350 kg; `piece` products just always store a
    # whole number in the same field (e.g. 5.000).
    quantity = models.DecimalField(default=0, blank=True, max_digits=10, decimal_places=3,
                                   verbose_name='stock quantity',
                                   help_text='Can be negative if items are sold before delivery is recorded')
    supplier = models.ManyToManyField(Suppliers, through='ProductSupplier', blank=True, related_name='products')
    is_recipe = models.BooleanField(
        default=False, blank=True, verbose_name='Made from other products (recipe)',
        help_text=(
            'If checked, selling this product deducts stock from its ingredients '
            'instead of its own quantity (e.g. a cappuccino made from milk + coffee).'
        ),
    )
    # Set once the historical-sales backfill has run for this recipe (see
    # STORA.sales.tasks.backfill_recipe_ingredient_stock) so it only ever
    # runs once, even if the recipe is saved again later.
    ingredients_backfilled_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Product"
        verbose_name_plural = "Products"
        ordering = ['internal_code']
        indexes = [
            GinIndex(fields=['name'], name='product_name_trgm_idx', opclasses=['gin_trgm_ops']),
            GinIndex(fields=['internal_code'], name='product_code_trgm_idx', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return self.name


class RecipeIngredient(models.Model):
    """One ingredient line for a `Product` with `is_recipe=True`.
    `quantity` is in the ingredient's own stock unit (kg for a weight
    ingredient, whole pcs for a piece ingredient) -- how much of it goes
    into ONE unit of the recipe product."""

    recipe = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='recipe_ingredients')
    ingredient = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='used_in_recipes')
    quantity = models.DecimalField(max_digits=10, decimal_places=3,
                                   validators=[MinValueValidator(Decimal('0.001'))],
                                   verbose_name='Quantity per unit',
                                   help_text="In the ingredient's own unit (kg for weight, pcs for piece).")

    class Meta:
        verbose_name = 'Recipe Ingredient'
        verbose_name_plural = 'Recipe Ingredients'
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['recipe', 'ingredient'], name='unique_recipe_ingredient'),
        ]

    def clean(self):
        if self.ingredient_id and self.ingredient_id == self.recipe_id:
            raise ValidationError('A product cannot be an ingredient of itself.')
        if self.ingredient_id and getattr(self.ingredient, 'is_recipe', False):
            raise ValidationError('A recipe cannot be used as an ingredient of another recipe.')

    def __str__(self):
        return f'{self.recipe} <- {self.quantity} x {self.ingredient}'


class ProductSupplier(models.Model):
    """Through model for Product<->Suppliers -- `position` marks which
    supplier is primary (1) vs. an alternate (2, 3, ...) for this product."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='product_suppliers')
    supplier = models.ForeignKey(Suppliers, on_delete=models.CASCADE, related_name='product_suppliers')
    position = models.PositiveSmallIntegerField(default=1, verbose_name='Position',
                                help_text='1 = primary supplier, 2+ = alternates')

    class Meta:
        verbose_name = 'Product Supplier'
        verbose_name_plural = 'Product Suppliers'
        ordering = ['position', 'id']
        constraints = [
            models.UniqueConstraint(fields=['product', 'supplier'], name='unique_product_supplier'),
        ]

    def __str__(self):
        return f'{self.product} - {self.supplier} (#{self.position})'