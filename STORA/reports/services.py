from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Sum

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems
from STORA.products.models import Product, RecipeIngredient
from STORA.revisions.models import RevisionAttributes, RevisionItems
from STORA.sales.models import RefundItems, SaleItems


def stock_as_of(as_of_date):
    """Reconstructs each (non-recipe) product's stock quantity as it stood
    at the end of `as_of_date`.

    There's no day-by-day snapshot table -- Product.quantity is just
    "whatever it currently is". So instead this starts from the CURRENT
    quantity and undoes every dated stock movement that happened strictly
    AFTER `as_of_date`: sales, refunds, deliveries/write-offs/scrap,
    completed revisions, and recipe-ingredient consumption. Every one of those has a
    well-defined signed effect on Product.quantity (the forward version of
    the same events is in products/views.py::ProductHistoryView), so
    "as-of quantity" = current - sum(signed effect of events after the date).

    Deliveries are matched by `document_date` (the date printed on the
    invoice), not `time_of_delivery` (when it was entered into the system)
    -- a delivery entered today but dated yesterday must still count for an
    "as of yesterday" query, matching how paperwork actually arrives.

    Recipe products are excluded from the result (their own `quantity`
    field is never touched by sales, see CLAUDE.md -- it wouldn't mean
    anything here), but a recipe SALE still counts as consumption against
    its ingredients. Recipe sales never create a SaleItems row for the
    ingredient itself (adjust_stock_for_sale moves the ingredient's own
    quantity directly), so ingredient consumption has to be reconstructed
    from recipe sales x the recipe's CURRENT composition -- this assumes
    the recipe hasn't been edited since, same "no historical versioning"
    limitation the app already accepts for prices/revisions elsewhere.
    """
    products = list(Product.objects.filter(is_recipe=False).select_related('category'))
    current_by_id = {product.pk: product.quantity for product in products}
    net_change_after = {product.pk: Decimal('0') for product in products}

    sales_after = (
        SaleItems.objects
        .filter(sale_item__is_recipe=False, sale__time_of_sale__date__gt=as_of_date)
        .values('sale_item_id')
        .annotate(total=Sum('sale_quantity'))
    )
    for row in sales_after:
        product_id = row['sale_item_id']
        if product_id in net_change_after:
            net_change_after[product_id] -= row['total']

    # A refund gives stock back -- RefundItems.save() applies it via
    # adjust_stock_for_sale(product, -refund_quantity), literally a
    # negative sale (see that model's own comment). Undoing a refund that
    # happened after as_of_date therefore needs the OPPOSITE sign from
    # undoing a sale above. Missing this was a real bug (caught by
    # reports/tests.py::StockMovementTotalsTests reconciliation check,
    # 2026-09-30) -- any product with a refund made "as of" quantities
    # wrong for every date before that refund.
    refunds_after = (
        RefundItems.objects
        .filter(original_item__sale_item__is_recipe=False, refund__time_of_refund__date__gt=as_of_date)
        .values('original_item__sale_item_id')
        .annotate(total=Sum('refund_quantity'))
    )
    for row in refunds_after:
        product_id = row['original_item__sale_item_id']
        if product_id in net_change_after:
            net_change_after[product_id] += row['total']

    deliveries_after = (
        DeliveryItems.objects
        .filter(delivery__document_date__gt=as_of_date)
        .values('delivery_item_id', 'delivery__movement_type')
        .annotate(total=Sum('delivery_quantity'))
    )
    for row in deliveries_after:
        product_id = row['delivery_item_id']
        if product_id not in net_change_after:
            continue
        sign = -1 if row['delivery__movement_type'] in DeliveryAttributes.OUTGOING_MOVEMENT_TYPES else 1
        net_change_after[product_id] += sign * row['total']

    revisions_after = (
        RevisionItems.objects
        .filter(
            revision__status=RevisionAttributes.STATUS_COMPLETED,
            revision__completed_at__date__gt=as_of_date,
        )
        .annotate(delta=F('found_quantity') - F('system_quantity_at_start'))
        .values('product_id')
        .annotate(total=Sum('delta'))
    )
    for row in revisions_after:
        product_id = row['product_id']
        if product_id in net_change_after:
            net_change_after[product_id] += row['total']

    recipe_sales_after = dict(
        SaleItems.objects
        .filter(sale_item__is_recipe=True, sale__time_of_sale__date__gt=as_of_date)
        .values('sale_item_id')
        .annotate(total=Sum('sale_quantity'))
        .values_list('sale_item_id', 'total')
    )
    if recipe_sales_after:
        ingredient_links = RecipeIngredient.objects.filter(
            recipe_id__in=recipe_sales_after.keys()
        ).values_list('recipe_id', 'ingredient_id', 'quantity')
        for recipe_id, ingredient_id, quantity_per_unit in ingredient_links:
            if ingredient_id not in net_change_after:
                continue
            net_change_after[ingredient_id] -= quantity_per_unit * recipe_sales_after[recipe_id]

    # Refunding a recipe product gives its ingredients back (same
    # adjust_stock_for_sale(-refund_quantity) path as the plain-product
    # refund above) -- mirrors recipe_sales_after with the opposite sign.
    recipe_refunds_after = dict(
        RefundItems.objects
        .filter(original_item__sale_item__is_recipe=True, refund__time_of_refund__date__gt=as_of_date)
        .values('original_item__sale_item_id')
        .annotate(total=Sum('refund_quantity'))
        .values_list('original_item__sale_item_id', 'total')
    )
    if recipe_refunds_after:
        ingredient_links = RecipeIngredient.objects.filter(
            recipe_id__in=recipe_refunds_after.keys()
        ).values_list('recipe_id', 'ingredient_id', 'quantity')
        for recipe_id, ingredient_id, quantity_per_unit in ingredient_links:
            if ingredient_id not in net_change_after:
                continue
            net_change_after[ingredient_id] += quantity_per_unit * recipe_refunds_after[recipe_id]

    return [
        {
            'product': product,
            'quantity_as_of': current_by_id[product.pk] - net_change_after[product.pk],
            'current_quantity': current_by_id[product.pk],
        }
        for product in products
    ]


def _sum_by_product(queryset, product_field, quantity_field):
    """{product_id: total} from a queryset grouped/summed by the given
    fields -- the same `.values(...).annotate(Sum(...))` shape used
    throughout this module, factored out since stock_movement_totals needs
    it five times over."""
    return dict(
        queryset.values(product_field).annotate(total=Sum(quantity_field)).values_list(product_field, 'total')
    )


def stock_movement_totals(start_date, end_date):
    """Per-(non-recipe)-product movement breakdown for [start_date,
    end_date] (both inclusive): opening balance, each kind of movement as
    its own column, and closing balance -- the "what moved this stock" a
    shop owner asks for on a period, not just a single day's snapshot.

    Opening/closing both reuse stock_as_of (the one place that already
    knows how to reconstruct a historical quantity -- see its own
    docstring for why that's a "replay backwards from today" and not a
    stored ledger). The movement columns are fresh range-scoped aggregates
    using the exact same date fields and movement types stock_as_of itself
    undoes when reconstructing "after a date" -- just windowed to
    [start_date, end_date] here instead of "> as_of_date" -- so the two
    stay consistent with each other without sharing code (stock_as_of is
    already working/tested elsewhere; not touched here).

    Each movement value is a positive MAGNITUDE (how much happened), except
    'revised' which keeps its sign since a stock-count correction can go
    either way -- matches how the report displays them ("Sold 2" reads
    naturally; which direction a column moves stock is implied by the
    column itself, not a +/- in the cell).

    Only products with at least one nonzero movement in the period are
    returned -- an all-zero row for every untouched product in the catalog
    would swamp the table on a quiet week.
    """
    opening_by_id = {row['product'].pk: row['quantity_as_of'] for row in stock_as_of(start_date - timedelta(days=1))}
    closing_rows = stock_as_of(end_date)
    closing_by_id = {row['product'].pk: row['quantity_as_of'] for row in closing_rows}
    product_by_id = {row['product'].pk: row['product'] for row in closing_rows}

    sold = _sum_by_product(
        SaleItems.objects.filter(sale_item__is_recipe=False, sale__time_of_sale__date__range=(start_date, end_date)),
        'sale_item_id', 'sale_quantity',
    )
    refunded = _sum_by_product(
        RefundItems.objects.filter(refund__time_of_refund__date__range=(start_date, end_date)),
        'original_item__sale_item_id', 'refund_quantity',
    )
    delivered = _sum_by_product(
        DeliveryItems.objects.filter(
            delivery__movement_type=DeliveryAttributes.MOVEMENT_DELIVERY,
            delivery__document_date__range=(start_date, end_date),
        ),
        'delivery_item_id', 'delivery_quantity',
    )
    written_off = _sum_by_product(
        DeliveryItems.objects.filter(
            delivery__movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            delivery__document_date__range=(start_date, end_date),
        ),
        'delivery_item_id', 'delivery_quantity',
    )
    scrapped = _sum_by_product(
        DeliveryItems.objects.filter(
            delivery__movement_type=DeliveryAttributes.MOVEMENT_SCRAP,
            delivery__document_date__range=(start_date, end_date),
        ),
        'delivery_item_id', 'delivery_quantity',
    )
    revised = dict(
        RevisionItems.objects
        .filter(
            revision__status=RevisionAttributes.STATUS_COMPLETED,
            revision__completed_at__date__range=(start_date, end_date),
        )
        .exclude(found_quantity=F('system_quantity_at_start'))
        .annotate(delta=F('found_quantity') - F('system_quantity_at_start'))
        .values('product_id').annotate(total=Sum('delta'))
        .values_list('product_id', 'total')
    )

    # Same reconstruction stock_as_of uses for recipe-ingredient
    # consumption (no discrete log of it exists anywhere, see that
    # function's docstring) -- current recipe composition x recipe sales
    # in the period, not a historical one.
    recipe_sales = dict(
        SaleItems.objects
        .filter(sale_item__is_recipe=True, sale__time_of_sale__date__range=(start_date, end_date))
        .values('sale_item_id').annotate(total=Sum('sale_quantity'))
        .values_list('sale_item_id', 'total')
    )
    recipe_consumed = {}
    if recipe_sales:
        ingredient_links = RecipeIngredient.objects.filter(
            recipe_id__in=recipe_sales.keys()
        ).values_list('recipe_id', 'ingredient_id', 'quantity')
        for recipe_id, ingredient_id, quantity_per_unit in ingredient_links:
            recipe_consumed[ingredient_id] = (
                recipe_consumed.get(ingredient_id, Decimal('0')) + quantity_per_unit * recipe_sales[recipe_id]
            )

    moved_product_ids = (
        set(sold) | set(refunded) | set(delivered) | set(written_off)
        | set(scrapped) | set(revised) | set(recipe_consumed)
    )

    rows = []
    for product_id in moved_product_ids:
        product = product_by_id.get(product_id)
        if product is None:
            # stock_as_of returns every non-recipe product unconditionally,
            # so this shouldn't happen -- guards against a future change
            # there silently breaking this instead of raising somewhere
            # confusing.
            continue
        rows.append({
            'product': product,
            'opening': opening_by_id.get(product_id, Decimal('0')),
            'delivered': delivered.get(product_id, Decimal('0')),
            'sold': sold.get(product_id, Decimal('0')),
            'refunded': refunded.get(product_id, Decimal('0')),
            'scrapped': scrapped.get(product_id, Decimal('0')),
            'written_off': written_off.get(product_id, Decimal('0')),
            'revised': revised.get(product_id, Decimal('0')),
            'recipe_consumed': recipe_consumed.get(product_id, Decimal('0')),
            'closing': closing_by_id.get(product_id, Decimal('0')),
        })
    rows.sort(key=lambda row: row['product'].name)
    return rows
