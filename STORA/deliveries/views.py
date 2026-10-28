from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.postgres.search import TrigramSimilarity
from django.db import DataError, IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.views.decorators.http import require_POST
from django.utils.translation import gettext as _

import json
import logging

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.core.session_service import NEW_PRODUCT_STATE_FIELDS, extract_formset_state
from STORA.core.utils import (
    build_cashier_operation_state,
    build_restore_formset_data,
    get_cashier_operation_type,
    multi_token_icontains_q,
)
from STORA.deliveries import tab_state
from STORA.deliveries.excel_import import build_template_workbook, parse_delivery_import
from STORA.deliveries.forms import (
    DeliveryForms, DeliveryItemFormSet, DocumentTypeForm, WriteOffForm, ScrapForm, ScrapReasonForm,
)
from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType, ScrapReason
from STORA.products.models import Barcode, Product, ProductAttribute, ProductSupplier, Suppliers, TaxGroup
from STORA.products.views import get_free_internal_codes

logger = logging.getLogger(__name__)


def _items_initial_from_rows(rows):
    """Shapes a list of per-row dicts (from extract_formset_state, a draft's
    stored formset_data, or built straight from DeliveryItems below) into
    what the Tabulator grid in `_delivery_items_table.html` expects. The JS
    resolves `delivery_item` against the already-loaded `products_data` for
    display (code/name/unit_type) -- this only needs to carry the bare
    model values."""
    items = []
    for row in rows:
        is_pending = row.get('new_product') == '1'
        if not row.get('delivery_item') and not is_pending:
            continue
        item = {
            'id': row.get('id') or '',
            'delivery_item': row.get('delivery_item') or '',
            'delivery_quantity': row.get('delivery_quantity') or '',
            'price_at_delivery': row.get('price_at_delivery') or '',
            'total_price_row': row.get('total_price_row') or '',
            'expiry_date': row.get('expiry_date') or '',
            'source_item': row.get('source_item') or '',
            'scrap_reason': row.get('scrap_reason') or '',
        }
        if is_pending:
            # An Excel-imported row that never became a real product (the
            # Save failed, or the page was reloaded from the draft) -- the
            # grid rebuilds it from these fields (pendingRowFromSaved).
            item.update({name: row.get(name, '') for name in NEW_PRODUCT_STATE_FIELDS})
        items.append(item)
    return items


def _items_initial_from_instance(delivery):
    return _items_initial_from_rows([
        {
            'id': item.pk,
            'delivery_item': item.delivery_item_id,
            'delivery_quantity': item.delivery_quantity,
            'price_at_delivery': item.price_at_delivery,
            'total_price_row': item.total_price_row,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else '',
            'source_item': item.source_item_id or '',
            'scrap_reason': item.scrap_reason_id or '',
        }
        for item in delivery.items.all()
    ])


class PendingProductError(Exception):
    """An Excel-imported "new product" row couldn't be turned into a real
    Product at Save time -- the message is already translated and safe to
    show the clerk as-is (see deliveries_add)."""


_ITEM_SUFFIXES = (
    'id', 'delivery_item', 'delivery_quantity', 'price_at_delivery',
    'total_price_row', 'expiry_date', 'source_item', 'scrap_reason', 'DELETE',
)


def _rewrite_item_rows(post_data, total_forms, kept_rows):
    """Replaces every items-<i>-* key in `post_data` with `kept_rows`
    (dicts of _ITEM_SUFFIXES values), re-indexed from 0, and fixes
    TOTAL_FORMS. Every old key is cleared first so a dropped row's stale
    keys can't linger under an index that now belongs to a different row."""
    for index in range(total_forms):
        prefix = f'items-{index}-'
        for key in list(post_data.keys()):
            if key.startswith(prefix):
                del post_data[key]
    for new_index, row in enumerate(kept_rows):
        prefix = f'items-{new_index}-'
        for suffix, value in row.items():
            post_data[prefix + suffix] = value
    post_data['items-TOTAL_FORMS'] = str(len(kept_rows))
    return post_data


def _without_pending_rows(post_data):
    """A copy of `post_data` with every Excel-imported "new product" row
    removed -- for the error redisplay, where those rows were rolled back
    and never got a real delivery_item id: binding them to the formset would
    just print a meaningless "delivery_item: This field is required" for
    each one, on top of the real problem."""
    post_data = post_data.copy()
    try:
        total_forms = int(post_data.get('items-TOTAL_FORMS', 0))
    except ValueError:
        return post_data
    kept_rows = [
        {suffix: post_data.get(f'items-{index}-{suffix}', '') for suffix in _ITEM_SUFFIXES}
        for index in range(total_forms)
        if post_data.get(f'items-{index}-new_product') != '1'
    ]
    return _rewrite_item_rows(post_data, total_forms, kept_rows)


def _formset_for_redisplay(data, initial=None):
    """The items formset to render alongside the grid. Pending Excel rows are
    stripped from `data` first (see _without_pending_rows); if that leaves
    NOTHING (every row was pending), the formset is left unbound instead --
    a bound empty one would print "You must add at least one delivery item"
    for a grid that visibly has rows."""
    stripped = _without_pending_rows(data)
    had_pending = any(
        data.get(f'items-{i}-new_product') == '1'
        for i in range(int(data.get('items-TOTAL_FORMS', 0) or 0))
    )
    if had_pending and stripped.get('items-TOTAL_FORMS') == '0':
        return DeliveryItemFormSet(prefix='items')
    return DeliveryItemFormSet(stripped, prefix='items', initial=initial)


def _clean_pending_attributes(raw_json, attributes_by_id):
    """`new_product_attributes` hidden field (a JSON object, attribute id ->
    value, built from the Excel import -- see excel_import) -> the dict to
    store on Product.attributes. It arrives through a plain POST, so it's
    re-checked here rather than trusted: ids that aren't a real
    ProductAttribute, blank/non-string values, and values outside a
    choices-restricted attribute's list are all dropped, same rules as
    ProductForms' own attribute fields. A malformed blob just yields {} --
    attributes are optional, never a reason to fail the whole delivery."""
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for attribute_id, value in data.items():
        attribute = attributes_by_id.get(str(attribute_id))
        if attribute is None or not isinstance(value, str):
            continue
        value = value.strip()
        if not value or (attribute.choices and value not in attribute.choices):
            continue
        cleaned[str(attribute.pk)] = value
    return cleaned


def _materialize_pending_products(post_data):
    """Excel import (see excel_import.parse_delivery_import) adds rows to
    the delivery grid for products that don't exist YET -- the grid marks
    each with a `new_product` hidden field instead of a real `delivery_item`
    id, carrying the raw imported values alongside (new_product_name,
    new_product_category, ...). Nothing about that touches the database
    while the clerk is still reviewing/editing the delivery on screen --
    this is where it finally happens, called right before the normal
    DeliveryItemFormSet is built, so by the time that formset runs every
    row looks like an ordinary "existing product" row to it.

    `post_data` is a mutable copy of request.POST -- mutated in place and
    returned. Runs inside the same transaction as the delivery save
    (deliveries_add wraps this), so a failed save can't leave orphan
    products behind.

    Quantity decides what happens to each pending row, worked out with the
    shop owner up front:
      - 0 (or blank) -- create the product, nothing else. The row is
        dropped from post_data entirely and every row after it shifted
        down to fill the gap (see the re-indexing below) -- leaving it in
        place as an all-blank row was tried first and does NOT get
        silently skipped by Django's formset the way a genuinely absent
        row does: DeliveryItemForm.__init__ forces delivery_item/
        delivery_quantity to required=True, which turned out to survive
        the formset's own empty-extra-form short-circuit (confirmed
        live -- "This field is required" on a row that was supposed to be
        invisible to the formset entirely).
      - >0 -- create the product AND deliver it: `new_product` is swapped
        for a real `delivery_item` id, so the rest of this row flows
        through the SAME formset/DeliveryItems.save() path (stock delta,
        auto ProductSupplier link off the delivery's own supplier, etc.)
        as a normal manually-added row -- nothing about that logic is
        duplicated here.

    The imported "Main Supplier" is a property of the PRODUCT (its primary
    catalog supplier, same as ProductSupplier position=1 on the product
    form), not of this particular delivery -- linked explicitly here,
    regardless of quantity, independent of whatever DeliveryItems.save()'s
    own auto-link off the delivery's own supplier field does."""
    total_forms_key = 'items-TOTAL_FORMS'
    try:
        total_forms = int(post_data.get(total_forms_key, 0))
    except ValueError:
        return post_data

    item_suffixes = _ITEM_SUFFIXES
    next_code_counter = None
    kept_rows = []
    attributes_by_id = {str(a.pk): a for a in ProductAttribute.objects.all()}
    tax_groups_by_id = {str(g.pk): g for g in TaxGroup.objects.all()}
    default_tax_group = TaxGroup.default()

    for index in range(total_forms):
        prefix = f'items-{index}-'

        if post_data.get(prefix + 'new_product') != '1':
            kept_rows.append({suffix: post_data.get(prefix + suffix, '') for suffix in item_suffixes})
            continue

        name = (post_data.get(prefix + 'new_product_name') or '').strip()
        if not name:
            continue  # Nothing usable to create -- drop the row outright.

        code = (post_data.get(prefix + 'new_product_code') or '').strip()
        # A code collision (blank to begin with, or someone else just took
        # this number) falls back to the next free one rather than hitting
        # a hard IntegrityError mid-save.
        while not code or Product.objects.filter(internal_code=code).exists():
            if next_code_counter is None:
                next_code_counter = get_free_internal_codes()[1]
            code = str(next_code_counter)
            next_code_counter += 1

        try:
            delivery_price = Decimal(post_data.get(prefix + 'new_product_delivery_price') or '0')
        except InvalidOperation:
            delivery_price = Decimal('0')
        try:
            sell_price = Decimal(post_data.get(prefix + 'new_product_sell_price') or '0')
        except InvalidOperation:
            sell_price = Decimal('0')
        # Product.sell_price is required (MinValueValidator(0.01)) -- the
        # Excel column is optional, so a blank one falls back to the
        # delivery price, and only if THAT is also blank to a placeholder
        # minimum a person fixes later via the normal product edit screen.
        if sell_price <= 0:
            sell_price = delivery_price if delivery_price > 0 else Decimal('0.01')

        category_id = post_data.get(prefix + 'new_product_category') or None
        supplier_id = post_data.get(prefix + 'new_product_supplier') or None
        unit_type = post_data.get(prefix + 'new_product_unit_type') or Product.PIECE
        if unit_type not in (Product.PIECE, Product.WEIGHT):
            unit_type = Product.PIECE

        try:
            product = Product.objects.create(
                internal_code=code,
                name=name,
                unit_type=unit_type,
                delivery_price=delivery_price if delivery_price > 0 else None,
                sell_price=sell_price,
                category_id=category_id,
                # Posted id re-checked (comes from a plain POST); anything
                # blank/unknown falls back to the shop's default group,
                # same as a new product on the product form.
                tax_group=tax_groups_by_id.get(post_data.get(prefix + 'new_product_tax_group') or '') or default_tax_group,
                attributes=_clean_pending_attributes(post_data.get(prefix + 'new_product_attributes'), attributes_by_id),
            )

            for position, barcode_code in enumerate(
                [b for b in (
                    post_data.get(prefix + 'new_product_barcode_1'),
                    post_data.get(prefix + 'new_product_barcode_2'),
                    post_data.get(prefix + 'new_product_barcode_3'),
                ) if b],
                start=1,
            ):
                Barcode.objects.create(product=product, code=barcode_code, position=position)

            if supplier_id:
                ProductSupplier.objects.get_or_create(product=product, supplier_id=supplier_id, defaults={'position': 1})
        except (IntegrityError, DataError):
            # Name/code/barcode are UNIQUE (or too long for the column) --
            # most often two rows of the SAME file sharing a name or
            # barcode, or a name that already exists in the catalog. The
            # whole delivery is rolled back by the caller; say exactly
            # WHICH product tripped it instead of a generic failure.
            raise PendingProductError(_(
                'Could not create the new product "%(name)s" -- its name, code or barcode is probably '
                'already used by another product, or by another row of this delivery. Nothing was '
                'saved: remove or fix that row and try again.'
            ) % {'name': name})

        raw_qty = post_data.get(prefix + 'delivery_quantity') or '0'
        try:
            quantity = Decimal(raw_qty)
        except InvalidOperation:
            quantity = Decimal('0')

        if quantity > 0:
            row = {suffix: post_data.get(prefix + suffix, '') for suffix in item_suffixes}
            row['delivery_item'] = str(product.pk)
            kept_rows.append(row)
        # else: product-only row -- created above, nothing added to
        # kept_rows, so it never reaches the formset at all.

    return _rewrite_item_rows(post_data, total_forms, kept_rows)


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
def deliveries_add(request, movement_type=DeliveryAttributes.MOVEMENT_DELIVERY):
    is_write_off = movement_type == DeliveryAttributes.MOVEMENT_WRITE_OFF
    is_scrap = movement_type == DeliveryAttributes.MOVEMENT_SCRAP
    if is_scrap:
        form_class = ScrapForm
    elif is_write_off:
        form_class = WriteOffForm
    else:
        form_class = DeliveryForms

    products_data = list(Product.objects.values('id', 'internal_code', 'name', 'delivery_price', 'sell_price', 'unit_type', 'tax_group__rate'))
    barcodes_data = list(Barcode.objects.values('code', 'product_id'))
    scrap_reasons_data = list(ScrapReason.objects.values('id', 'name')) if is_scrap else []

    formset_prefix = 'items'
    operation_type = get_cashier_operation_type(request.path)
    # Which of the 3 parallel drafts (tab_state) this page belongs to. A
    # POST names its tab explicitly (hidden `draft_tab`, rendered below) so
    # a second browser window can't redirect the save to the wrong draft.
    if request.method == "POST" and request.POST.get('draft_tab') in tab_state.TAB_IDS:
        draft_tab = request.POST['draft_tab']
    elif request.method == "POST":
        draft_tab = tab_state.get_active_tab(request)
    else:
        draft_tab = tab_state.resolve_tab_for_page(request, operation_type)
    state = tab_state.get_tab_draft(request, draft_tab)
    delivery_draft = None
    formset_initial = []
    delivery_items_initial = []
    selected_supplier_name = ''

    if request.method == "POST":
        # A mutated COPY of request.POST -- _materialize_pending_products
        # creates real Products for any Excel-imported "new_product" rows
        # and rewrites them to look like normal existing-product rows (see
        # that function's own docstring). Done inside the same atomic
        # block as the actual save, rolled back together if form/formset
        # validation fails for any OTHER reason (a missing Document Date,
        # say) -- nothing pending ever gets left behind by a save that
        # didn't actually go through. The original, unmutated request.POST
        # is what the error-redisplay branch below still uses -- a pending
        # row that never got created has no real delivery_item id to
        # rebuild the grid from, so it's dropped from the redisplayed grid
        # on a validation failure (a known Phase 1 gap: re-import if that
        # happens, rather than the larger work of round-tripping pending-
        # product data through the session draft too).
        post_data = request.POST.copy()
        delivery = None
        try:
            with transaction.atomic():
                _materialize_pending_products(post_data)
                form = form_class(post_data, current_user=request.user)
                formset = DeliveryItemFormSet(post_data, prefix=formset_prefix)

                if form.is_valid() and formset.is_valid():
                    delivery = form.save(commit=False)
                    delivery.receiver = request.user
                    delivery.movement_type = movement_type
                    delivery.save()

                    formset.instance = delivery
                    formset.save()
                else:
                    transaction.set_rollback(True)
        except PendingProductError as exc:
            # A specific, already-explained reason (see _materialize_pending_
            # products) -- shown as-is instead of the generic message below.
            messages.error(request, str(exc))
            delivery = None
        except Exception:
            # Anything unexpected here (a DB error, a bug) must not behave
            # any differently from "the form was invalid" as far as the
            # warehouse clerk is concerned -- transaction.atomic() has
            # already rolled back whatever partial write was in progress
            # (nothing bad is left in the database), and `delivery` stays
            # None, so execution falls through to the exact same
            # redisplay-with-draft-saved code below that a validation
            # failure already uses. Logged here (not swallowed) so a real
            # bug still surfaces in logs/stora.log for someone to fix.
            logger.exception(
                'Unexpected error saving %s (movement_type=%s)',
                operation_type, movement_type,
            )
            messages.error(request, _(
                'Saving failed due to an unexpected error. Nothing was lost -- '
                'your entered data is still below. Please try again, or contact '
                'the administrator if it keeps happening.'
            ))
            delivery = None

        if delivery is not None:
            tab_state.clear_tab_draft(request, draft_tab)
            if movement_type != DeliveryAttributes.MOVEMENT_DELIVERY:
                return redirect('delivery_details', pk=delivery.pk)
            # deliveries_list isn't in the nav menu anymore (superseded by
            # the unified Stock Movements report) -- send the warehouse
            # clerk straight into a fresh delivery instead, same pattern as
            # sales_add redirecting to itself after completing a sale.
            return redirect('delivery_add')

        extracted_state = extract_formset_state(request.POST, formset_prefix)
        delivery_items_initial = _items_initial_from_rows(extracted_state.get('forms', []))
        posted_supplier_id = request.POST.get('supplier') or None
        selected_supplier = Suppliers.objects.filter(pk=posted_supplier_id).first() if posted_supplier_id else None
        selected_supplier_name = selected_supplier.name if selected_supplier else ''
        delivery_draft = build_cashier_operation_state(
            operation_type=operation_type,
            path=request.path,
            data={
                'receiver': request.POST.get('receiver', ''),
                'supplier': request.POST.get('supplier', ''),
                'time_of_delivery': request.POST.get('time_of_delivery', ''),
                'document_type': request.POST.get('document_type', ''),
                'document_number': request.POST.get('document_number', ''),
                'document_date': request.POST.get('document_date', ''),
                'comment': request.POST.get('comment', ''),
            },
            formset_data=extracted_state,
            active=True,
        )
        tab_state.set_tab_draft(request, draft_tab, delivery_draft)

        form = form_class(request.POST, current_user=request.user)
        formset_initial = extracted_state.get('forms', [])
        formset = _formset_for_redisplay(request.POST, initial=formset_initial)
    else:
        if state and state.get('type') == operation_type and state.get('active'):
            form = form_class(initial=state.get('data', {}), current_user=request.user)
            formset_initial = state.get('formset_data', {}).get('forms', []) or [{}]
            # Pending Excel rows live only in the grid (delivery_items_initial
            # below), not in the bound formset (see _formset_for_redisplay).
            restore_post_data = build_restore_formset_data(formset_prefix, formset_initial)
            delivery_items_initial = _items_initial_from_rows(formset_initial)
            draft_supplier_id = state.get('data', {}).get('supplier') or None
            selected_supplier = Suppliers.objects.filter(pk=draft_supplier_id).first() if draft_supplier_id else None
            selected_supplier_name = selected_supplier.name if selected_supplier else ''

            formset = _formset_for_redisplay(restore_post_data)
            delivery_draft = state
        else:
            form = form_class(current_user=request.user)
            formset = DeliveryItemFormSet(prefix=formset_prefix)

    context = {
        'form': form,
        'formset': formset,
        'formset_prefix': formset_prefix,
        'products_data': products_data,
        'barcodes_data': barcodes_data,
        'delivery_draft': delivery_draft,
        'delivery_formset_initial_count': len(formset_initial) if formset_initial else 0,
        'delivery_items_initial': delivery_items_initial,
        'selected_supplier_name': selected_supplier_name,
        'delivery_tab': draft_tab,
        'delivery_tabs_summary': tab_state.tabs_summary(request),
        'movement_type': movement_type,
        'is_write_off': is_write_off,
        'is_scrap': is_scrap,
        'scrap_reasons_data': scrap_reasons_data,
    }
    return render(request, 'deliveries/delivery_add.html', context)


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
def delivery_import_template(request):
    """"Download template" on the New Delivery screen's Import from Excel
    modal -- a blank .xlsx with just the expected header row, generated on
    the fly (not a static file) so it can never drift out of sync with
    excel_import.COLUMN_HEADERS."""
    wb = build_template_workbook()
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="stora_delivery_import_template.xlsx"'
    wb.save(response)
    return response


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
@require_POST
def delivery_import_excel(request):
    """"Import from Excel" on the New Delivery screen -- parses/matches the
    uploaded file (see excel_import.parse_delivery_import) and hands the
    result back as JSON; the grid's own JS adds each row exactly like a
    normal search-to-add or "Delivery from Order" pick. Nothing here
    touches the database -- see _materialize_pending_products for where a
    "new product" row actually becomes one, at Save time."""
    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': _('No file was uploaded.')}, status=400)
    result = parse_delivery_import(uploaded)
    return JsonResponse(result)


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
@require_POST
def delivery_draft_save(request):
    payload = json.loads(request.body.decode('utf-8'))
    form_data = payload.get('form_data', {})
    formset_data = payload.get('formset_data', {})
    # The same draft-save endpoint is shared by the delivery, write-off and
    # scrap add screens -- the caller tells us which page it was on so the
    # draft is tagged with the right operation_type, otherwise a write-off
    # draft would get saved (and later resumed) as a delivery.
    path = payload.get('path') or '/deliveries/add/'
    # ...and which of the 3 parallel drafts it belongs to (falls back to
    # the session's active tab for an older page that doesn't send one).
    tab = payload.get('tab')
    if tab not in tab_state.TAB_IDS:
        tab = tab_state.get_active_tab(request)

    draft = build_cashier_operation_state(
        operation_type=get_cashier_operation_type(path) or 'delivery',
        path=path,
        data=form_data,
        formset_data=formset_data,
        active=True,
    )
    tab_state.set_tab_draft(request, tab, draft)
    return JsonResponse({'status': 'ok'})


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
@require_POST
def switch_delivery_tab(request):
    """Switches which of the 3 parallel drafts is active and tells the JS
    which page to load next: the target tab's own screen if it holds a
    draft (a write-off draft must reopen on the write-off page), otherwise
    the page the clerk is already on (an empty tab takes whatever kind of
    operation they start there)."""
    payload = json.loads(request.body.decode('utf-8'))
    tab = payload.get('tab')
    if tab not in tab_state.TAB_IDS:
        return JsonResponse({'error': _('Invalid tab.')}, status=400)
    draft = tab_state.get_tab_draft(request, tab)
    if tab_state.draft_has_items(draft) and get_cashier_operation_type(draft.get('path', '')):
        next_path = draft['path']
    else:
        current_path = payload.get('path') or ''
        next_path = current_path if get_cashier_operation_type(current_path) else reverse('delivery_add')
    tab_state.set_active_tab(request, tab)
    return JsonResponse({'status': 'ok', 'path': next_path})


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
def cancel_delivery_tab(request):
    """The add screens' "Cancel" -- discards just the draft on the tab the
    page was rendered for (?tab=) and lands on a fresh screen of the same
    kind, leaving the other two tabs untouched."""
    tab = request.GET.get('tab')
    if tab not in tab_state.TAB_IDS:
        tab = tab_state.get_active_tab(request)
    tab_state.clear_tab_draft(request, tab)
    tab_state.set_active_tab(request, tab)
    return redirect({
        DeliveryAttributes.MOVEMENT_WRITE_OFF: 'writeoff_add',
        DeliveryAttributes.MOVEMENT_SCRAP: 'scrap_add',
    }.get(request.GET.get('type'), 'delivery_add'))


class DeliveryDetailView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    permission_required = 'deliveries.view_deliveryattributes'
    model = DeliveryAttributes
    template_name = 'deliveries/delivery_details.html'
    context_object_name = 'delivery'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = self.object.items.select_related(
            'delivery_item', 'delivery_item__tax_group', 'scrap_reason',
        ).all()
        context['delivered_items'] = items
        context['delivery_items_data'] = [self._item_row(item) for item in items]
        return context

    @staticmethod
    def _item_row(item):
        """Same read-only shape as the editable items grid on Add/Edit
        (_delivery_items_table.html) -- VAT-exclusive price via the
        product's own tax_group, plus the product's CURRENT Sell Price/
        Markup % (a live snapshot, not what it was at delivery time)."""
        product = item.delivery_item
        qty = item.delivery_quantity or Decimal('0')
        with_vat = item.price_at_delivery or Decimal('0.00')
        rate = product.tax_group.rate if product.tax_group else Decimal('0')
        without_vat = with_vat / (Decimal('1') + rate / Decimal('100')) if rate else with_vat
        sell_price = product.sell_price or Decimal('0.00')
        markup = (sell_price - with_vat) / with_vat * Decimal('100') if with_vat else Decimal('0')
        return {
            'internal_code': product.internal_code,
            'name': product.name,
            'delivery_quantity': float(qty),
            'price_at_delivery': float(with_vat),
            'price_without_vat': float(without_vat.quantize(Decimal('0.01'))),
            'total_price_row': float(item.total_price_row or 0),
            'total_without_vat': float((qty * without_vat).quantize(Decimal('0.01'))),
            'sell_price': float(sell_price),
            'markup_percent': float(markup.quantize(Decimal('0.1'))),
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else '',
            'scrap_reason_name': item.scrap_reason.name if item.scrap_reason else '',
        }


class DeliveryListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'deliveries.view_deliveryattributes'
    model = DeliveryAttributes
    template_name = 'deliveries/deliveries_list.html'
    context_object_name = 'deliveries'

    def get_queryset(self):
        # Deliveries only -- write-offs live in the same table now but
        # don't belong on this (deprecated, kept alive only as a redirect
        # target -- see ROADMAP.md) list.
        return super().get_queryset().filter(movement_type=DeliveryAttributes.MOVEMENT_DELIVERY)


@login_required
@permission_required('deliveries.change_deliveryattributes', raise_exception=True)
def delivery_edit(request, pk):
    delivery = get_object_or_404(DeliveryAttributes, pk=pk)
    is_write_off = delivery.movement_type == DeliveryAttributes.MOVEMENT_WRITE_OFF
    is_scrap = delivery.movement_type == DeliveryAttributes.MOVEMENT_SCRAP
    if is_scrap:
        form_class = ScrapForm
    elif is_write_off:
        form_class = WriteOffForm
    else:
        form_class = DeliveryForms

    products_data = list(Product.objects.values('id', 'internal_code', 'name', 'delivery_price', 'sell_price', 'unit_type', 'tax_group__rate'))
    barcodes_data = list(Barcode.objects.values('code', 'product_id'))
    scrap_reasons_data = list(ScrapReason.objects.values('id', 'name')) if is_scrap else []

    formset_prefix = 'items'

    if request.method == "POST":
        form = form_class(request.POST, instance=delivery)
        formset = DeliveryItemFormSet(request.POST, instance=delivery, prefix=formset_prefix)
        saved = False

        if form.is_valid() and formset.is_valid():
            try:
                # Wrapped in atomic() so a crash partway through formset.save()
                # (which writes one DeliveryItems row at a time) can never
                # leave this delivery half-updated in the database -- it
                # didn't have this before, meaning such a crash used to be
                # able to apply some item changes but not others.
                with transaction.atomic():
                    # Read by DeliveryItemRemoval's post_delete signal
                    # (models.py) so a removed line's audit record knows who
                    # removed it -- the signal itself has no access to
                    # request.user. Set on each about-to-be-deleted form's
                    # instance, not the whole formset, since only some rows
                    # (if any) are actually being removed.
                    for item_form in formset.deleted_forms:
                        item_form.instance._removed_by = request.user
                    form.save()
                    formset.save()
                saved = True
            except Exception:
                # Same reasoning as deliveries_add: an unexpected failure
                # here must fall through to the normal "show the form again
                # with what was submitted" path below, not a raw 500 that
                # throws away the edit the clerk just typed.
                logger.exception('Unexpected error saving edited delivery #%s', pk)
                messages.error(request, _(
                    'Saving failed due to an unexpected error. Nothing was lost -- '
                    'your edits are still below. Please try again, or contact the '
                    'administrator if it keeps happening.'
                ))

        if saved:
            return redirect('delivery_details', pk=delivery.pk)

        extracted_state = extract_formset_state(request.POST, formset_prefix)
        delivery_items_initial = _items_initial_from_rows(extracted_state.get('forms', []))
    else:
        form = form_class(instance=delivery)
        formset = DeliveryItemFormSet(instance=delivery, prefix=formset_prefix)
        delivery_items_initial = _items_initial_from_instance(delivery)

    context = {
        'delivery': delivery,
        'form': form,
        'formset': formset,
        'formset_prefix': formset_prefix,
        'products_data': products_data,
        'barcodes_data': barcodes_data,
        'delivery_items_initial': delivery_items_initial,
        'movement_type': delivery.movement_type,
        'is_write_off': is_write_off,
        'is_scrap': is_scrap,
        'scrap_reasons_data': scrap_reasons_data,
    }
    return render(request, 'deliveries/delivery_edit.html', context)


class DeliveryDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'deliveries.delete_deliveryattributes'
    model = DeliveryAttributes
    template_name = 'deliveries/delivery_confirm_delete.html'
    context_object_name = 'delivery'
    success_url = reverse_lazy('deliveries_list')


class DocumentTypeListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'deliveries.view_documenttype'
    model = DocumentType
    template_name = 'deliveries/document_type_list.html'
    context_object_name = 'document_types'


class DocumentTypeCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    # Manager-only on purpose -- unlike Category/TaxGroup/Supplier, there's
    # no "+ New document type" shortcut from the delivery form linking here
    # with ?popup=1, so this is always a full page visit from the dedicated
    # Document Types screen, never a popup tab.
    permission_required = 'deliveries.add_documenttype'
    model = DocumentType
    form_class = DocumentTypeForm
    template_name = 'deliveries/document_type_form.html'
    success_url = reverse_lazy('document_type_list')


class DocumentTypeUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    permission_required = 'deliveries.change_documenttype'
    model = DocumentType
    form_class = DocumentTypeForm
    template_name = 'deliveries/document_type_form.html'
    context_object_name = 'document_type'
    success_url = reverse_lazy('document_type_list')


class DocumentTypeDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'deliveries.delete_documenttype'
    model = DocumentType
    template_name = 'deliveries/document_type_confirm_delete.html'
    context_object_name = 'document_type'
    success_url = reverse_lazy('document_type_list')


class ScrapReasonListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'deliveries.view_scrapreason'
    model = ScrapReason
    template_name = 'deliveries/scrap_reason_list.html'
    context_object_name = 'scrap_reasons'


class ScrapReasonCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    # Manager-only, same reasoning as DocumentTypeCreateView -- no shortcut
    # popup from the scrap form, always a full page visit from the
    # dedicated Scrap Reasons screen.
    permission_required = 'deliveries.add_scrapreason'
    model = ScrapReason
    form_class = ScrapReasonForm
    template_name = 'deliveries/scrap_reason_form.html'
    success_url = reverse_lazy('scrap_reason_list')


class ScrapReasonUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    permission_required = 'deliveries.change_scrapreason'
    model = ScrapReason
    form_class = ScrapReasonForm
    template_name = 'deliveries/scrap_reason_form.html'
    context_object_name = 'scrap_reason'
    success_url = reverse_lazy('scrap_reason_list')


class ScrapReasonDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'deliveries.delete_scrapreason'
    model = ScrapReason
    template_name = 'deliveries/scrap_reason_confirm_delete.html'
    context_object_name = 'scrap_reason'
    success_url = reverse_lazy('scrap_reason_list')


class BatchSearchView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """Backs the "pick a delivered batch to scrap" search on the scrap add/
    edit screen (scrap variant 1) -- searches DELIVERY items that have an
    expiry_date, i.e. an actual received batch, not write-off/scrap rows
    (which would make scrapping a scrap meaningless) and not deliveries
    that never recorded an expiry (nothing to "pick" there, see variant 2:
    free entry on the same screen for that case)."""

    permission_required = 'deliveries.view_deliveryattributes'
    RESULTS_LIMIT = 30

    def get(self, request):
        query = request.GET.get('q', '').strip()
        batches = DeliveryItems.objects.filter(
            delivery__movement_type=DeliveryAttributes.MOVEMENT_DELIVERY,
            expiry_date__isnull=False,
        ).select_related('delivery_item')

        if query:
            batches = (
                batches
                .filter(multi_token_icontains_q(query, ['delivery_item__name', 'delivery_item__internal_code']))
                .annotate(similarity=TrigramSimilarity('delivery_item__name', query))
                .order_by('-similarity', 'expiry_date')
            )
        else:
            batches = batches.order_by('expiry_date')

        batches = batches[:self.RESULTS_LIMIT]

        return JsonResponse({'results': [
            {
                'id': batch.pk,
                'product_id': batch.delivery_item_id,
                'internal_code': batch.delivery_item.internal_code,
                'name': batch.delivery_item.name,
                'expiry_date': batch.expiry_date.isoformat(),
                'delivery_quantity': float(batch.delivery_quantity),
            }
            for batch in batches
        ]})
