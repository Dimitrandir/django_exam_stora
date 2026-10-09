from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from STORA.accounts.models import CompanyProfile
from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.products.models import Barcode, Product, ProductAttribute, Suppliers
from STORA.products.views import flatten_product_attributes
from STORA.reports.ai_service import AIReportsNotConfigured
from STORA.sales.models import SaleItems

from STORA.orders.ai_service import suggest_order_quantities
from STORA.orders.forms import OrderPickerForm
from STORA.orders.models import OrderAttributes, OrderItems


def _valid_positions(raw_positions):
    """MultipleChoiceField.cleaned_data comes back as a list of STRINGS
    ('1'/'2'/'3') -- normalized here to a deduped, sorted list of ints,
    dropping anything outside the real choices."""
    valid = {c[0] for c in OrderAttributes.POSITION_CHOICES}
    result = set()
    for raw in raw_positions:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value in valid:
            result.add(value)
    return sorted(result)


def _candidate_products(supplier, positions):
    """Products this supplier carries at any of the given ranks (see
    ProductSupplier.position) -- `positions` is a list, since the compose
    screen now lets more than one tier be ticked at once. Excludes
    recipes (never delivered/ordered directly, same exclusion as
    products.ingredient_search) and archived products (discontinued --
    nothing to reorder)."""
    return Product.objects.filter(
        product_suppliers__supplier=supplier,
        product_suppliers__position__in=positions,
        is_recipe=False,
        is_archived=False,
    ).distinct().order_by('internal_code')


def _barcode_by_product(products):
    """{product_id: primary (lowest-position, non-scale) barcode code}."""
    barcode_by_product = {}
    for row in (
        Barcode.objects.filter(product__in=products, is_scale_code=False)
        .order_by('product_id', 'position').values('product_id', 'code')
    ):
        barcode_by_product.setdefault(row['product_id'], row['code'])
    return barcode_by_product


def _parse_id_list(raw):
    """'1,2,3' -> [1, 2, 3] -- shared by every "Load into X" preload param
    across the app (see products_list.html's bulk-actions bar); tolerates
    a trailing comma/blank entries, drops anything non-numeric instead of
    raising."""
    result = []
    for part in (raw or '').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


def _build_candidate_lines(supplier, positions, start_date, end_date, existing_qty_by_product=None, extra_product_ids=None):
    """Plain-dict rows for the compose/edit screen's Tabulator grid --
    sales quantity/distinct-sale-count are computed fresh from SaleItems
    every time (the period is whatever was just picked), never stored.
    `existing_qty_by_product` ({product_id: Decimal}), when given (order_edit),
    pre-fills Requested Qty for products already on the order instead of 0.

    `extra_product_ids`, when given (the Products grid's "Load into Order"
    bulk action, see order_new below), forces those specific products onto
    the grid even if they're not one of this supplier's own candidates --
    same "explicitly hand-picked, not filtered by the usual rule" reasoning
    as the compose screen's own "+ Add product" button. Deliberately still
    excludes recipes/archived products (never orderable), same as
    _candidate_products."""
    existing_qty_by_product = existing_qty_by_product or {}
    products = list(_candidate_products(supplier, positions))
    if extra_product_ids:
        existing_ids = {p.pk for p in products}
        extra_ids = [pid for pid in extra_product_ids if pid not in existing_ids]
        if extra_ids:
            products += list(Product.objects.filter(pk__in=extra_ids, is_recipe=False, is_archived=False))
    if not products:
        return []

    sales_totals = {
        row['sale_item_id']: row
        for row in (
            SaleItems.objects.filter(
                sale_item__in=products,
                sale__time_of_sale__date__range=(start_date, end_date),
            )
            .values('sale_item_id')
            .annotate(total_qty=Sum('sale_quantity'), sale_count=Count('sale', distinct=True))
        )
    }
    barcode_by_product = _barcode_by_product(products)
    product_attributes = list(ProductAttribute.objects.all())

    return [
        {
            'product_id': product.pk,
            'internal_code': product.internal_code,
            'name': product.name,
            'barcode': barcode_by_product.get(product.pk, ''),
            # Needed client-side so the Qty editor can pick step 1 vs
            # 0.001 per row -- see CLAUDE.md's HTML5 number-input min/step
            # pitfall.
            'unit_type': product.unit_type,
            # Current stock, so the person drafting the order can see how
            # much is already on the shelf next to how much sold in the
            # period -- helps judge whether the sales figure alone is
            # already covered by what's on hand.
            'in_stock': float(product.quantity),
            'sold_qty': float((sales_totals.get(product.pk) or {}).get('total_qty') or 0),
            'sale_count': (sales_totals.get(product.pk) or {}).get('sale_count') or 0,
            'requested_qty': float(existing_qty_by_product.get(product.pk, 0)),
            # Needed client-side for the grid's own Value column/bottom-sum
            # -- same "line total, computed live off today's catalog price"
            # reasoning as the Orders list's own Total column (_order_totals).
            'delivery_price': float(product.delivery_price) if product.delivery_price is not None else 0,
            **flatten_product_attributes(product, product_attributes),
        }
        for product in products
    ]


def _save_items_from_post(request):
    """Shared by order_new/order_edit -- reads every qty_<product_id> field
    off the POST body and returns the list of (product, qty>0) pairs to
    save. Doesn't touch the DB itself; callers decide create-vs-replace.

    Deliberately NOT limited to the supplier/period candidate list: the
    compose/edit grid's "+ Add product" button lets the user pull in any
    catalog product (see _order_items_table.html), so a qty_<id> field can
    point at a product that never went through _candidate_products at
    all."""
    raw_by_id = {}
    for key, raw in request.POST.items():
        if not key.startswith('qty_'):
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            product_id = int(key[len('qty_'):])
            qty = Decimal(raw)
        except (ValueError, InvalidOperation):
            continue
        if qty <= 0:
            continue
        raw_by_id[product_id] = qty

    if not raw_by_id:
        return []

    products_by_id = Product.objects.in_bulk(raw_by_id.keys())
    return [(products_by_id[pid], qty) for pid, qty in raw_by_id.items() if pid in products_by_id]


@login_required
@permission_required('orders.add_orderattributes', raise_exception=True)
def order_new(request):
    """Compose screen -- GET with supplier/position(s)/period in the
    querystring loads the candidate table (see _build_candidate_lines);
    POST (same fields, resubmitted as hidden inputs, plus one qty_<id>
    field per row the JS builds before submit -- same rebuildHiddenInputs
    pattern as the Deliveries grid) saves the order.

    ?preload=<id1>,<id2>,... (Products grid's "Load into: Order" bulk
    action) can't skip the supplier picker the way Deliveries/Revision do
    -- an order always needs exactly one supplier (a required FK), which
    picking several arbitrary products doesn't determine on its own. So
    the picker form carries `preload` through as a hidden field (see
    order_new.html) across the GET round-trip it already does when the
    person submits supplier/position/period, and once that lands here
    with a valid supplier, those specific products get forced onto the
    grid via _build_candidate_lines' extra_product_ids -- same idea as the
    grid's own "+ Add product" button, just pre-filled instead of typed."""
    # Bound only once the picker's own fields are actually present -- a
    # bare `?preload=1,2` (arriving fresh from the Products grid, before
    # any supplier has been picked) must still render as an untouched
    # form, not one that's already failed validation for fields nobody
    # had a chance to fill in yet (confirmed live: `request.GET or None`
    # alone treated `preload`'s mere presence as "the picker was
    # submitted", showing "This field is required" on page load).
    is_picker_submission = request.method == 'POST' or 'supplier' in request.GET
    data = request.POST if request.method == 'POST' else (request.GET if is_picker_submission else None)
    form = OrderPickerForm(data)
    error = None
    lines_data = None
    selected_supplier_name = ''
    preload = request.GET.get('preload', '')
    preload_count = len(_parse_id_list(preload))

    if form.is_valid():
        supplier = form.cleaned_data['supplier']
        positions = _valid_positions(form.cleaned_data['supplier_position'])
        start_date = form.cleaned_data['start_date']
        end_date = form.cleaned_data['end_date']
        selected_supplier_name = supplier.name

        if request.method == 'POST':
            to_create = _save_items_from_post(request)

            if not to_create:
                error = _('Enter at least one requested quantity.')
            else:
                with transaction.atomic():
                    order = OrderAttributes.objects.create(
                        supplier=supplier, period_start=start_date, period_end=end_date, created_by=request.user,
                    )
                    order.set_position_list(positions)
                    order.save(update_fields=['supplier_position'])
                    OrderItems.objects.bulk_create([
                        OrderItems(order=order, product=product, requested_quantity=qty)
                        for product, qty in to_create
                    ])
                return redirect('order_details', pk=order.pk)

        lines_data = _build_candidate_lines(
            supplier, positions, start_date, end_date, extra_product_ids=_parse_id_list(preload),
        )

    return render(request, 'orders/order_new.html', {
        'form': form,
        'selected_supplier_name': selected_supplier_name,
        'lines_data': lines_data,
        'preload': preload,
        'preload_count': preload_count,
        'error': error,
        'ai_available': bool(settings.ANTHROPIC_API_KEY),
        'product_attributes': ProductAttribute.objects.all(),
    })


@login_required
@permission_required('orders.change_orderattributes', raise_exception=True)
def order_edit(request, pk):
    """Adjust an existing order's quantities (or add newly-relevant
    candidates from the same supplier/positions) -- supplier/positions/
    period stay locked to what the order was originally built with (shown
    read-only), only quantities change. Saving fully REPLACES the order's
    items from whatever's non-zero in the grid, same "delete all, recreate"
    approach as a recipe's ingredient list, simpler than diffing."""
    order = get_object_or_404(OrderAttributes, pk=pk)
    positions = order.get_position_list()
    error = None

    if request.method == 'POST':
        to_save = _save_items_from_post(request)

        if not to_save:
            error = _('Enter at least one requested quantity.')
        else:
            with transaction.atomic():
                order.items.all().delete()
                OrderItems.objects.bulk_create([
                    OrderItems(order=order, product=product, requested_quantity=qty)
                    for product, qty in to_save
                ])
            return redirect('order_details', pk=order.pk)

    existing_qty_by_product = {item.product_id: item.requested_quantity for item in order.items.all()}
    lines_data = _build_candidate_lines(
        order.supplier, positions, order.period_start, order.period_end,
        existing_qty_by_product=existing_qty_by_product,
    )

    return render(request, 'orders/order_edit.html', {
        'order': order,
        'lines_data': lines_data,
        'error': error,
        'ai_available': bool(settings.ANTHROPIC_API_KEY),
        'product_attributes': ProductAttribute.objects.all(),
    })


@login_required
@permission_required('orders.add_orderattributes', raise_exception=True)
@require_POST
def order_ai_suggest(request):
    """Called from the compose screen's "AI Suggest Quantities" button --
    never saves anything, just returns suggested quantities for the JS to
    drop into the Requested Qty column (the user still reviews/edits and
    Saves normally afterward, same as a manually-typed value would)."""
    supplier = Suppliers.objects.filter(pk=request.POST.get('supplier_id')).first()
    start_date = parse_date(request.POST.get('start_date', ''))
    end_date = parse_date(request.POST.get('end_date', ''))
    positions = _valid_positions(request.POST.getlist('supplier_position'))

    if not supplier or not start_date or not end_date or not positions:
        return JsonResponse({'error': _('Pick a supplier, position, and period first.')}, status=400)

    candidates_qs = _candidate_products(supplier, positions)
    candidates = [{'id': p.pk, 'name': p.name, 'internal_code': p.internal_code} for p in candidates_qs]
    if not candidates:
        return JsonResponse({'suggestions': []})

    try:
        suggestions = suggest_order_quantities(supplier.name, positions, start_date, end_date, candidates)
    except AIReportsNotConfigured:
        return JsonResponse({'error': _('AI mode is not set up yet -- ANTHROPIC_API_KEY is missing from .env.')}, status=400)
    except Exception as exc:
        return JsonResponse({'error': _('AI request failed: %(error)s') % {'error': exc}}, status=500)

    return JsonResponse({'suggestions': suggestions})


# How far back "AI Draft All" looks at sales when deciding what a supplier
# needs -- fixed rather than user-chosen, since the whole point of this
# button is running unattended across every supplier at once (see
# order_ai_draft_all). Same window as the compose screen's own "Days back"
# convenience default.
AI_DRAFT_LOOKBACK_DAYS = 30


@login_required
@permission_required('orders.add_orderattributes', raise_exception=True)
@require_POST
def order_ai_draft_all(request):
    """Phase 2 "AI Draft All" -- one button on the Orders list, asked for
    live: an order per supplier, decided entirely by Claude, not a human
    picking a candidate list first. Unlike order_ai_suggest (which only
    fills in quantities for products a human already chose to show on
    screen), this hands Claude a supplier's ENTIRE assortment (all three
    ProductSupplier positions at once -- there's no per-supplier position
    picker here) and lets it decide both WHICH products need reordering and
    HOW MUCH, via the same suggest_order_quantities prompt ("0 for a
    product with no sales" already covers "don't include this").

    Every order this creates lands as status=DRAFT -- never mixed in as if
    a human had built it -- so a person still has to open it (order_edit,
    already reachable from order_list) and explicitly order_confirm or
    order_discard it. Suppliers with nothing worth ordering (no candidate
    products at all, or Claude suggested nothing) are silently skipped, not
    left as an empty draft.

    Runs synchronously in the request -- one AI call per supplier in a
    loop -- same accepted tradeoff as every other AI call in this app
    (order_ai_suggest, reports.ai_service): fine at this shop's supplier
    count, and the alternative (Celery) would need a "job running..."
    polling UI for what is, in practice, a once-in-a-while manual button."""
    if not settings.ANTHROPIC_API_KEY:
        return redirect('order_list')

    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=AI_DRAFT_LOOKBACK_DAYS)
    positions = [OrderAttributes.POSITION_PRIMARY, OrderAttributes.POSITION_SECONDARY, OrderAttributes.POSITION_TERTIARY]

    created = 0
    skipped = 0
    for supplier in Suppliers.objects.all():
        candidates_qs = list(_candidate_products(supplier, positions))
        if not candidates_qs:
            continue

        candidates = [{'id': p.pk, 'name': p.name, 'internal_code': p.internal_code} for p in candidates_qs]
        products_by_id = {p.pk: p for p in candidates_qs}

        try:
            suggestions = suggest_order_quantities(supplier.name, positions, start_date, end_date, candidates)
        except AIReportsNotConfigured:
            break
        except Exception:
            skipped += 1
            continue

        to_create = [
            (products_by_id[s['product_id']], Decimal(str(s['quantity'])))
            for s in suggestions
            if s.get('quantity', 0) > 0 and s.get('product_id') in products_by_id
        ]
        if not to_create:
            skipped += 1
            continue

        with transaction.atomic():
            order = OrderAttributes.objects.create(
                supplier=supplier, period_start=start_date, period_end=end_date,
                created_by=request.user, status=OrderAttributes.STATUS_DRAFT,
            )
            order.set_position_list(positions)
            order.save(update_fields=['supplier_position'])
            OrderItems.objects.bulk_create([
                OrderItems(order=order, product=product, requested_quantity=qty)
                for product, qty in to_create
            ])
        created += 1

    return redirect(f"{reverse('order_list')}?ai_drafted={created}&ai_skipped={skipped}")


@login_required
@permission_required('orders.change_orderattributes', raise_exception=True)
@require_POST
def order_confirm(request, pk):
    """Marks a DRAFT order (always AI-made, see order_ai_draft_all) as
    reviewed and real. Same permission tier as editing an order's
    quantities -- Warehouse's day-to-day review/adjust/confirm job, not
    Manager-only."""
    order = get_object_or_404(OrderAttributes, pk=pk, status=OrderAttributes.STATUS_DRAFT)
    order.status = OrderAttributes.STATUS_CONFIRMED
    order.save(update_fields=['status'])
    return redirect('order_details', pk=order.pk)


@login_required
@permission_required('orders.delete_orderattributes', raise_exception=True)
@require_POST
def order_discard(request, pk):
    """Deletes a DRAFT order outright -- restricted to status=DRAFT on
    purpose (the URL can't be pointed at a real, human-built order by
    mistake). Manager-only, same tier as every other delete in this app
    (Warehouse can edit/confirm but not delete)."""
    order = get_object_or_404(OrderAttributes, pk=pk, status=OrderAttributes.STATUS_DRAFT)
    order.delete()
    return redirect('order_list')


@login_required
@permission_required('orders.view_orderattributes', raise_exception=True)
def order_picker_json(request):
    """Feeds the "Delivery from Order" modal on delivery_add.html -- a
    short, searchable list of recent orders to pick one from. Client-side
    substring filter (same reasoning as the compose screen's own supplier
    picker isn't needed here -- a shop's own order history is small enough
    that fetching the last 50 and filtering in JS is simpler than another
    trigram-search endpoint)."""
    orders = OrderAttributes.objects.select_related('supplier').order_by('-order_date', '-pk')[:50]
    return JsonResponse({
        'results': [
            {
                'id': order.pk,
                'order_number': order.order_number,
                'supplier': order.supplier.name,
                'order_date': order.order_date.strftime('%d.%m.%Y'),
                'item_count': order.items.count(),
            }
            for order in orders
        ],
    })


@login_required
@permission_required('orders.view_orderattributes', raise_exception=True)
def order_items_json(request, pk):
    """Feeds the Deliveries screen's "Delivery from Order" picker (see
    deliveries.views/delivery_add.html) -- the whole order's supplier +
    product/quantity list, for preloading straight into a new delivery's
    grid. Read-only, changes nothing here."""
    order = get_object_or_404(OrderAttributes, pk=pk)
    items = order.items.select_related('product')
    return JsonResponse({
        'order_number': order.order_number,
        'supplier_id': order.supplier_id,
        'supplier_name': order.supplier.name,
        'items': [
            {'product_id': item.product_id, 'quantity': float(item.requested_quantity)}
            for item in items
        ],
    })


def _order_totals(orders):
    """{order_id: total value} -- requested_quantity * the product's
    current delivery_price, summed per order. Not stored anywhere (same
    "compute live" reasoning as the compose screen's sales columns) --
    this is what the order would cost at TODAY's catalog prices, not a
    frozen snapshot of what it cost when drafted."""
    line_total = ExpressionWrapper(
        F('requested_quantity') * Coalesce(F('product__delivery_price'), Decimal('0')),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    rows = (
        OrderItems.objects.filter(order__in=orders)
        .values('order_id').annotate(line_total=line_total)
        .values('order_id').annotate(total=Sum('line_total'))
    )
    return {row['order_id']: float(row['total'] or 0) for row in rows}


class OrderListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'orders.view_orderattributes'
    model = OrderAttributes
    template_name = 'orders/order_list.html'
    context_object_name = 'orders'

    def get_queryset(self):
        return OrderAttributes.objects.select_related('supplier').prefetch_related('items')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        orders = list(context['orders'])
        totals = _order_totals(orders)
        context['orders_data'] = [
            {
                'id': order.pk,
                'order_number': order.order_number,
                'order_date': order.order_date.strftime('%d.%m.%Y'),
                'supplier': order.supplier.name,
                'supplier_position': order.get_supplier_position_display(),
                'period': f'{order.period_start:%d.%m.%Y} - {order.period_end:%d.%m.%Y}',
                'item_count': order.items.count(),
                'total_amount': totals.get(order.pk, 0),
                'status': order.get_status_display(),
                'is_draft': order.status == OrderAttributes.STATUS_DRAFT,
                'view_url': reverse('order_details', kwargs={'pk': order.pk}),
            }
            for order in orders
        ]
        context['ai_available'] = bool(settings.ANTHROPIC_API_KEY)
        return context


class OrderDetailView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    permission_required = 'orders.view_orderattributes'
    model = OrderAttributes
    template_name = 'orders/order_details.html'
    context_object_name = 'order'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = list(self.object.items.select_related('product').order_by('product__internal_code'))
        barcode_by_product = _barcode_by_product([item.product for item in items])
        context['items_data'] = [
            {
                'internal_code': item.product.internal_code,
                'name': item.product.name,
                'barcode': barcode_by_product.get(item.product_id, ''),
                'requested_qty': float(item.requested_quantity),
            }
            for item in items
        ]
        context['company'] = CompanyProfile.get_solo()
        return context
