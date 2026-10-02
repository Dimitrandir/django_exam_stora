from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import F, Sum
from django.db.models.functions import Coalesce

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems
from STORA.products.models import Product, ProductChangeLog, RecipeIngredient
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
        # quantity_before_complete is the change Complete REALLY applied;
        # older revisions (completed before that field existed) fall back to
        # the start-of-count snapshot, the best figure they have.
        .annotate(delta=F('found_quantity') - Coalesce('quantity_before_complete', 'system_quantity_at_start'))
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


def _average_delivery_price_by_id(end_date):
    """{product_id: weighted-average price_at_delivery} across real
    deliveries (movement_type=DELIVERY -- write-off/scrap have no cost
    paid, nothing to average) with document_date <= end_date. Unlike
    sell_price, this never needs reconstructing: every DeliveryItems row
    already carries its own historically-accurate price, so filtering by
    date and weighting by quantity is all "as of end_date" cost needs.
    Missing entirely for a product with no delivery by that date."""
    rows = (
        DeliveryItems.objects
        .filter(
            delivery__movement_type=DeliveryAttributes.MOVEMENT_DELIVERY,
            delivery__document_date__lte=end_date,
        )
        .values('delivery_item_id')
        .annotate(
            weighted_total=Sum(F('delivery_quantity') * F('price_at_delivery')),
            total_qty=Sum('delivery_quantity'),
        )
    )
    return {
        row['delivery_item_id']: row['weighted_total'] / row['total_qty']
        for row in rows if row['total_qty']
    }


def _sell_price_as_of(product, end_date, changes_by_product):
    """Reconstructs what `product.sell_price` was at the end of `end_date`
    -- sell_price is just a live field with no dated ledger of its own, so
    this walks ProductChangeLog (the only history it has) backward from
    the CURRENT value, undoing every logged change that happened strictly
    after end_date, same idea as stock_as_of but for a scalar instead of a
    running total. `changes_by_product`: {product_id: [ProductChangeLog,
    ...]} for field_name='sell_price', ascending by changed_at (batched by
    the caller -- see stock_movement_totals -- so this doesn't run its own
    query per product).

    Known gap, accepted rather than worked around: bulk "Apply Price"
    edits in the Products grid use QuerySet.update(), which bypasses
    ProductChangeLog entirely (see that model's own docstring) -- a price
    changed only that way won't be reflected here. Same pre-existing
    limitation ProductHistoryView already lives with.
    """
    changes = changes_by_product.get(product.pk)
    if not changes:
        return product.sell_price
    value = product.sell_price
    for change in reversed(changes):
        if change.changed_at.date() <= end_date:
            break
        try:
            value = Decimal(change.old_value)
        except (InvalidOperation, TypeError):
            break
    return value


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

    Also values both the OPENING and CLOSING balance (not every movement
    column -- scope confirmed with the shop owner): 'opening_sell_value'/
    'opening_purchase_value'/'closing_sell_value'/'closing_purchase_value'
    (+ their '..._no_vat' counterparts), purchase value `None` if the
    product was never delivered by the relevant date (nothing to average).
    Crucially, neither date is always "today" -- an accountant can ask for
    "наличност към 31.12.2025" -- so every value uses the price that was
    ACTUALLY in effect as of ITS OWN date (opening values as of the day
    before start_date, closing as of end_date), not today's: purchase
    value averages real historical price_at_delivery up to that date (see
    _average_delivery_price_by_id), sell value reconstructs the catalog
    price as of that date from ProductChangeLog (see _sell_price_as_of)
    since sell_price itself has no dated ledger.
    """
    opening_date = start_date - timedelta(days=1)
    opening_rows = stock_as_of(opening_date)
    opening_by_id = {row['product'].pk: row['quantity_as_of'] for row in opening_rows}
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
        # Same "what Complete REALLY changed" delta as stock_as_of above --
        # otherwise opening + movements wouldn't add up to closing whenever
        # something sold/arrived while a revision was still open.
        .annotate(delta=F('found_quantity') - Coalesce('quantity_before_complete', 'system_quantity_at_start'))
        .exclude(delta=0)
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

    # Valuation -- purchase side reuses actual historical delivery prices
    # (see _average_delivery_price_by_id), sell side reconstructs the
    # catalog price as of the relevant date (see _sell_price_as_of). Two
    # separate averages (opening_date vs end_date) since "as of" moves the
    # cutoff -- a delivery between the two dates counts toward closing's
    # average but must NOT count toward opening's. All batched up front
    # (one query each per date, or one query + a grouped in-memory walk)
    # rather than per product, even though only the products that end up
    # in `rows` actually get read from them.
    avg_delivery_price_opening_by_id = _average_delivery_price_by_id(opening_date)
    avg_delivery_price_closing_by_id = _average_delivery_price_by_id(end_date)
    sell_price_changes_by_id = {}
    for change in (
        ProductChangeLog.objects
        .filter(product_id__in=moved_product_ids, field_name='sell_price')
        .order_by('changed_at')
    ):
        sell_price_changes_by_id.setdefault(change.product_id, []).append(change)

    rows = []
    for product_id in moved_product_ids:
        product = product_by_id.get(product_id)
        if product is None:
            # stock_as_of returns every non-recipe product unconditionally,
            # so this shouldn't happen -- guards against a future change
            # there silently breaking this instead of raising somewhere
            # confusing.
            continue
        opening_qty = opening_by_id.get(product_id, Decimal('0'))
        closing_qty = closing_by_id.get(product_id, Decimal('0'))
        vat_divisor = Decimal('1') + product.tax_group.rate / Decimal('100') if product.tax_group else Decimal('1')

        opening_sell_price = _sell_price_as_of(product, opening_date, sell_price_changes_by_id)
        opening_sell_value = opening_qty * opening_sell_price
        avg_delivery_price_opening = avg_delivery_price_opening_by_id.get(product_id)
        opening_purchase_value = (
            opening_qty * avg_delivery_price_opening if avg_delivery_price_opening is not None else None
        )

        closing_sell_price = _sell_price_as_of(product, end_date, sell_price_changes_by_id)
        closing_sell_value = closing_qty * closing_sell_price
        avg_delivery_price_closing = avg_delivery_price_closing_by_id.get(product_id)
        closing_purchase_value = (
            closing_qty * avg_delivery_price_closing if avg_delivery_price_closing is not None else None
        )

        rows.append({
            'product': product,
            'opening': opening_qty,
            'delivered': delivered.get(product_id, Decimal('0')),
            'sold': sold.get(product_id, Decimal('0')),
            'refunded': refunded.get(product_id, Decimal('0')),
            'scrapped': scrapped.get(product_id, Decimal('0')),
            'written_off': written_off.get(product_id, Decimal('0')),
            'revised': revised.get(product_id, Decimal('0')),
            'recipe_consumed': recipe_consumed.get(product_id, Decimal('0')),
            'closing': closing_qty,
            'opening_sell_value': opening_sell_value,
            'opening_sell_value_no_vat': opening_sell_value / vat_divisor,
            'opening_purchase_value': opening_purchase_value,
            'opening_purchase_value_no_vat': (
                opening_purchase_value / vat_divisor if opening_purchase_value is not None else None
            ),
            'closing_sell_value': closing_sell_value,
            'closing_sell_value_no_vat': closing_sell_value / vat_divisor,
            'closing_purchase_value': closing_purchase_value,
            'closing_purchase_value_no_vat': (
                closing_purchase_value / vat_divisor if closing_purchase_value is not None else None
            ),
        })
    rows.sort(key=lambda row: row['product'].name)
    return rows
