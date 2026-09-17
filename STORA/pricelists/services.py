from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone

from STORA.pricelists.models import PriceListRule


def price_without_vat(gross_price, product):
    """Same "if the product has no tax group, without-VAT just mirrors
    with-VAT" convention as the Deliveries items grid and the product
    create/edit form's Prices section (see CLAUDE.md)."""
    if gross_price is None:
        return None
    if not product.tax_group_id:
        return gross_price
    divisor = Decimal('1') + (product.tax_group.rate / Decimal('100'))
    return (gross_price / divisor).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def rules_for_price_list(price_list):
    """One winning rule per product actually covered by THIS list (a
    product could be reachable by more than one of the list's own rules --
    e.g. a direct product rule and its whole category both in the same
    list -- the more specific one wins, same tie-break as resolve_prices()).
    Returns {product_id: rule}. Expands CATEGORY/SUPPLIER rules to their
    current member products -- used to render a price list's own read-only
    breakdown (revision_details-style), not for POS checkout resolution.
    """
    from STORA.products.models import Product, ProductSupplier

    rules = list(price_list.rules.select_related('product', 'category', 'supplier'))
    product_ids = {r.product_id for r in rules if r.scope_type == PriceListRule.SCOPE_PRODUCT}
    category_ids = {r.category_id for r in rules if r.scope_type == PriceListRule.SCOPE_CATEGORY}
    supplier_ids = {r.supplier_id for r in rules if r.scope_type == PriceListRule.SCOPE_SUPPLIER}

    products_by_category = defaultdict(list)
    if category_ids:
        for pid, cid in Product.objects.filter(category_id__in=category_ids).values_list('id', 'category_id'):
            products_by_category[cid].append(pid)

    products_by_supplier = defaultdict(list)
    if supplier_ids:
        for row in ProductSupplier.objects.filter(supplier_id__in=supplier_ids).values('product_id', 'supplier_id'):
            products_by_supplier[row['supplier_id']].append(row['product_id'])

    covering = defaultdict(list)
    for rule in rules:
        if rule.scope_type == PriceListRule.SCOPE_PRODUCT:
            covering[rule.product_id].append(rule)
        elif rule.scope_type == PriceListRule.SCOPE_CATEGORY:
            for pid in products_by_category.get(rule.category_id, []):
                covering[pid].append(rule)
        elif rule.scope_type == PriceListRule.SCOPE_SUPPLIER:
            for pid in products_by_supplier.get(rule.supplier_id, []):
                covering[pid].append(rule)

    specificity = PriceListRule.SCOPE_SPECIFICITY
    return {
        pid: min(candidates, key=lambda r: specificity[r.scope_type])
        for pid, candidates in covering.items()
    }


def _active_rules(on_date=None):
    on_date = on_date or timezone.localdate()
    return PriceListRule.objects.filter(
        price_list__is_deleted=False,
        price_list__start_date__lte=on_date,
        price_list__end_date__gte=on_date,
    ).select_related('price_list')


def _rule_sort_key(rule):
    # Higher priority wins; among equal priority, the more specific scope
    # (product beats category beats supplier) wins; among equal
    # specificity, the more recently created list wins. max() below picks
    # the largest of this tuple, so specificity is negated to make "more
    # specific" sort as "larger".
    return (
        rule.price_list.priority,
        -PriceListRule.SCOPE_SPECIFICITY[rule.scope_type],
        rule.price_list.created_at,
    )


def resolve_prices(products, on_date=None):
    """For each product in `products` (an iterable of Product instances --
    needs `.pk`/`.sell_price`/`.category_id`), works out which single
    PriceListRule (if any) currently wins for it, across every in-period,
    non-deleted PriceList, whether the rule reaches the product directly,
    via its category, or via one of its suppliers.

    Returns {product_id: {'price': Decimal, 'rule': PriceListRule}} --
    products with no covering rule are simply absent, so callers fall back
    to the product's own sell_price for anything not in this dict.

    Batches into a handful of queries total (not one per product), since
    this runs over the full catalog both for the Products grid and for the
    POS screen's price lookup.
    """
    products = list(products)
    if not products:
        return {}

    from STORA.products.models import ProductSupplier

    product_ids = [p.pk for p in products]
    category_ids = {p.category_id for p in products if p.category_id}

    supplier_by_product = defaultdict(list)
    for row in ProductSupplier.objects.filter(product_id__in=product_ids).values('product_id', 'supplier_id'):
        supplier_by_product[row['product_id']].append(row['supplier_id'])
    supplier_ids = {sid for sids in supplier_by_product.values() for sid in sids}

    scope_filter = (
        Q(scope_type=PriceListRule.SCOPE_PRODUCT, product_id__in=product_ids)
        | Q(scope_type=PriceListRule.SCOPE_CATEGORY, category_id__in=category_ids)
        | Q(scope_type=PriceListRule.SCOPE_SUPPLIER, supplier_id__in=supplier_ids)
    )
    rules = _active_rules(on_date).filter(scope_filter)

    product_rules = defaultdict(list)
    category_rules = defaultdict(list)
    supplier_rules = defaultdict(list)
    for rule in rules:
        if rule.scope_type == PriceListRule.SCOPE_PRODUCT:
            product_rules[rule.product_id].append(rule)
        elif rule.scope_type == PriceListRule.SCOPE_CATEGORY:
            category_rules[rule.category_id].append(rule)
        elif rule.scope_type == PriceListRule.SCOPE_SUPPLIER:
            supplier_rules[rule.supplier_id].append(rule)

    result = {}
    for product in products:
        candidates = list(product_rules.get(product.pk, []))
        if product.category_id:
            candidates += category_rules.get(product.category_id, [])
        for supplier_id in supplier_by_product.get(product.pk, []):
            candidates += supplier_rules.get(supplier_id, [])
        if not candidates:
            continue
        winner = max(candidates, key=_rule_sort_key)
        result[product.pk] = {'price': winner.effective_price(product.sell_price), 'rule': winner}
    return result


def resolve_price(product, on_date=None):
    """Single-product convenience wrapper around resolve_prices() -- prefer
    resolve_prices() when iterating many products (Products grid, POS
    catalog dump) to avoid one query round-trip per product. Returns
    (effective_price, winning_rule_or_None)."""
    matched = resolve_prices([product], on_date=on_date)
    if product.pk in matched:
        return matched[product.pk]['price'], matched[product.pk]['rule']
    return product.sell_price, None
