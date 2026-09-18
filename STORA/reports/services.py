from decimal import Decimal

from django.db.models import F, Sum

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems
from STORA.products.models import Product, RecipeIngredient
from STORA.revisions.models import RevisionAttributes, RevisionItems
from STORA.sales.models import SaleItems


def stock_as_of(as_of_date):
    """Reconstructs each (non-recipe) product's stock quantity as it stood
    at the end of `as_of_date`.

    There's no day-by-day snapshot table -- Product.quantity is just
    "whatever it currently is". So instead this starts from the CURRENT
    quantity and undoes every dated stock movement that happened strictly
    AFTER `as_of_date`: sales, deliveries/write-offs/scrap, completed
    revisions, and recipe-ingredient consumption. Every one of those has a
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

    return [
        {
            'product': product,
            'quantity_as_of': current_by_id[product.pk] - net_change_after[product.pk],
            'current_quantity': current_by_id[product.pk],
        }
        for product in products
    ]
