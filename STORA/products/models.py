from decimal import Decimal

from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from STORA.accounts.models import Employee



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
    # SET_NULL, not CASCADE -- mirrors Product.category below: deleting a
    # parent category just un-parents its subcategories (they become
    # top-level), it doesn't take them down with it.
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='subcategories', verbose_name='Parent Category')
    # Curated opt-in, not opt-out -- a new category defaults to hidden from
    # the POS screen until someone explicitly flags it. Controls the
    # category-folders panel in sales/sale_add.html, not the Products list.
    show_on_pos = models.BooleanField(
        default=False, verbose_name='Show on POS screen',
        help_text='If checked, this category appears as a folder button on the cash register screen.',
    )

    class Meta:
        indexes = [
            GinIndex(fields=['name'], name='category_name_trgm_idx', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return f"{self.name}"

    def get_descendant_ids(self):
        """Every subcategory id below this one, at any depth -- used to
        keep the parent picker (CategoryForm) from offering a cycle (a
        category becoming its own descendant's child)."""
        descendant_ids = []
        frontier = [self.pk]
        while frontier:
            frontier = list(Category.objects.filter(parent_id__in=frontier).values_list('pk', flat=True))
            descendant_ids.extend(frontier)
        return descendant_ids

class TaxGroup(models.Model):
    """A VAT rate a product can be assigned to (e.g. "Standard 20%",
    "Reduced 9%", "Zero-rated 0%") -- a manageable list like Category, not
    a hardcoded set, since VAT rules/rates can change by law. Product's
    own `delivery_price`/`sell_price` stay VAT-inclusive (gross) as before;
    this just lets the product create/edit form compute the matching
    VAT-exclusive amount live, and lets reports derive it later without a
    separately stored "without VAT" field."""

    name = models.CharField(max_length=60, unique=True)
    rate = models.DecimalField(
        max_digits=5, decimal_places=2, validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name='VAT rate (%)',
    )

    class Meta:
        verbose_name = 'Tax Group'
        verbose_name_plural = 'Tax Groups'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.rate}%)'


class Barcode(models.Model):
    code = models.CharField(max_length=13, null=True, blank=True, unique=True, verbose_name='Barcode number',
                            help_text='Scan the barcode or enter the EAN-13 number')
    position = models.PositiveSmallIntegerField(default=1, verbose_name='Position',
                                help_text='1 = primary barcode, 2+ = alternates')
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='barcode',
                                verbose_name='Linked Product')
    # A scale (weighing) barcode is 13 digits: a 7- or 8-digit product code
    # prefix, then a 5- or 4-digit quantity (grams or piece count depending
    # on the product's unit_type), then an EAN-13 check digit -- the actual
    # scanned code is different every time (it encodes the weighed amount),
    # so `code` here holds just the fixed PREFIX, not a full barcode.
    # See STORA/products/scale_barcode.py for the decode logic.
    is_scale_code = models.BooleanField(
        default=False, verbose_name='Scale (weighing) barcode prefix',
        help_text=(
            'Check this if the barcode above is a 7 or 8 digit scale-printed prefix '
            '(not a full barcode) -- the scale encodes weight/quantity after it.'
        ),
    )

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
    # Same curated opt-in idea as Category.show_on_pos -- lets a category
    # with many products show only a hand-picked subset of them as buttons
    # on the POS screen, instead of every product in it.
    show_on_pos = models.BooleanField(
        default=False, verbose_name='Show on POS screen',
        help_text='If checked, this product appears as a button when its category is opened on the cash register screen.',
    )
    tax_group = models.ForeignKey(
        TaxGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='products',
        verbose_name='Tax group',
    )
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

    def recalculate_ingredient_cost(self):
        """A recipe product is never itself delivered, so `delivery_price`
        ("Last Delivery Price") has no real value to hold for one -- instead
        it's repurposed to show the recipe's computed cost: the sum of each
        ingredient's own delivery_price times how much of it the recipe uses.
        Call this after (re)saving the recipe's RecipeIngredient rows."""
        if not self.is_recipe:
            return
        total = Decimal('0')
        for link in self.recipe_ingredients.select_related('ingredient'):
            if link.ingredient.delivery_price:
                total += link.quantity * link.ingredient.delivery_price
        if self.delivery_price != total:
            self.delivery_price = total
            self.save(update_fields=['delivery_price'])


# Fields worth an audit trail entry when they change -- deliberately not
# `quantity` (that already has its own history via SaleItems/DeliveryItems,
# see ProductHistoryView) and not the barcode/supplier/recipe formsets
# (each of those is its own set of rows, not a simple field edit).
PRODUCT_TRACKED_FIELDS = [
    'internal_code', 'name', 'unit_type', 'delivery_price', 'sell_price', 'category', 'tax_group', 'is_recipe',
]


class ProductChangeLog(models.Model):
    """One row per changed field per save -- who changed what on a Product
    and when, so "did someone touch this?" has an answer. Populated
    automatically by the pre_save/post_save signals below; catches edits
    from the Edit form and the Products grid's inline edit alike, since
    both go through Product.save(). Bulk actions (ProductBulkActionView)
    use QuerySet.update(), which bypasses model signals entirely -- not
    covered here."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='change_log')
    changed_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, related_name='product_changes')
    changed_at = models.DateTimeField(auto_now_add=True)
    field_name = models.CharField(max_length=50)
    old_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = 'Product Change'
        verbose_name_plural = 'Product Changes'
        ordering = ['-changed_at']

    def __str__(self):
        return f'{self.product} -- {self.field_name}: {self.old_value!r} -> {self.new_value!r}'


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


@receiver(pre_save, sender=Product)
def _snapshot_product_before_save(sender, instance, **kwargs):
    # Grabs the row as it stood in the DB right before this save, so
    # post_save below can diff against it. Nothing to compare a brand-new
    # (not-yet-saved) product against.
    if not instance.pk:
        instance._previous_state = None
        return
    try:
        instance._previous_state = Product.objects.get(pk=instance.pk)
    except Product.DoesNotExist:
        instance._previous_state = None


@receiver(post_save, sender=Product)
def _log_product_field_changes(sender, instance, created, **kwargs):
    previous = getattr(instance, '_previous_state', None)
    if created or previous is None:
        return
    changed_by = getattr(instance, '_changed_by', None)
    entries = []
    for field_name in PRODUCT_TRACKED_FIELDS:
        old_value = getattr(previous, field_name)
        new_value = getattr(instance, field_name)
        if old_value == new_value:
            continue
        entries.append(ProductChangeLog(
            product=instance, changed_by=changed_by, field_name=field_name,
            old_value='' if old_value is None else str(old_value),
            new_value='' if new_value is None else str(new_value),
        ))
    if entries:
        ProductChangeLog.objects.bulk_create(entries)