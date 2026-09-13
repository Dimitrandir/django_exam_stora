from datetime import timedelta

from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Max, Sum
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, DetailView, DeleteView
from django.views.decorators.http import require_POST
from STORA.sales.tasks import log_sale_completed

import json

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.core.session_service import extract_formset_state
from STORA.core.utils import build_restore_formset_data, dispatch_task
from STORA.products.models import Product, Barcode, Category
from STORA.sales.forms import SaleForms, SaleItemFormSet
from STORA.sales.models import SaleAttributes, SaleItems, PosPin, SaleItemVoidLog
from STORA.sales.tab_state import (
    TAB_IDS,
    clear_last_change,
    clear_tab_draft,
    get_active_tab,
    get_last_change,
    get_tab_draft,
    set_active_tab,
    set_last_change,
    set_tab_draft,
    tabs_summary,
)

# How many days back counts as "recently sold" for the top-3 most-popular
# products shown first when a category is opened on the POS screen (Фаза 2
# pinned-shortcuts feature). Arbitrary but reasonable -- easy to tune later.
POS_RECENT_SALES_DAYS = 7


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
def sales_add(request):
    # category_id feeds the category/subcategory quick-pick panel (Фаза 2) --
    # it groups these same products by category client-side, no extra
    # requests per click. show_on_pos is included so the panel's own JS can
    # filter to curated items -- the barcode/code search (findProductByCode)
    # deliberately still searches ALL products regardless of this flag, so
    # the full Product queryset (not a show_on_pos-filtered one) stays here.
    recent_cutoff = timezone.now() - timedelta(days=POS_RECENT_SALES_DAYS)
    recent_qty_by_product = dict(
        SaleItems.objects.filter(sale__time_of_sale__gte=recent_cutoff)
        .values('sale_item_id')
        .annotate(total_qty=Sum('sale_quantity'))
        .values_list('sale_item_id', 'total_qty')
    )
    products_data = [
        {**row, 'recent_qty': float(recent_qty_by_product.get(row['id'], 0))}
        for row in Product.objects.values(
            'id', 'internal_code', 'name', 'sell_price', 'unit_type', 'category_id', 'show_on_pos',
        )
    ]
    barcodes_data = list(Barcode.objects.values('code', 'product_id', 'is_scale_code'))
    categories_data = list(Category.objects.values('id', 'name', 'parent_id', 'show_on_pos'))
    pos_pins_data = [
        {
            'id': pin.id,
            'type': 'category' if pin.category_id else 'product',
            'target_id': pin.category_id or pin.product_id,
            'name': pin.category.name if pin.category_id else pin.product.name,
        }
        for pin in PosPin.objects.select_related('category', 'product').order_by('position')
    ]

    formset_prefix = 'items'
    # Each of the 3 basket tabs (see the header's tab buttons) keeps its own
    # independent draft -- a cashier parks an unfinished sale on one tab,
    # switches to another to ring up a different customer, then switches
    # back. Separate from the single-slot mechanism deliveries/write-off/
    # scrap share (see STORA.sales.tab_state for why).
    active_tab = get_active_tab(request)
    tab_draft = get_tab_draft(request, active_tab)
    sale_draft = None
    formset_initial = []
    sale_instance = SaleAttributes(cashier=request.user)

    if request.method == "POST":
        form = SaleForms(request.POST, current_user=request.user)
        formset = SaleItemFormSet(request.POST, instance=sale_instance, prefix=formset_prefix)

        if form.is_valid() and formset.is_valid():
            sale = form.save(commit=False)
            sale.cashier = request.user
            sale.save()

            formset.instance = sale
            formset.save()

            dispatch_task(log_sale_completed, sale.id)

            if sale.change_due is not None:
                set_last_change(request, active_tab, sale.change_due)
            clear_tab_draft(request, active_tab)
            # Back to a fresh New Sale screen, not the list -- a cashier
            # completing one sale is almost always about to ring up the
            # next customer, not review history. Stays on the SAME tab,
            # now empty -- matches "done with this customer" rather than
            # forcing a tab switch too.
            return redirect('sale_add')

        extracted_state = extract_formset_state(request.POST, formset_prefix)
        sale_draft = {'formset_data': extracted_state, 'active': True}
        set_tab_draft(request, active_tab, sale_draft)

        form = SaleForms(request.POST, current_user=request.user)
        formset_initial = extracted_state.get('forms', [])
        formset = SaleItemFormSet(
            request.POST,
            instance=sale_instance,
            prefix=formset_prefix,
            initial=formset_initial,
        )
    else:
        draft_forms = tab_draft.get('formset_data', {}).get('forms', []) if tab_draft and tab_draft.get('active') else []
        # A row with no sale_item is a ghost -- e.g. a stray draft-save
        # firing before a product actually landed in the cart -- and must
        # never be restored as if it were real. Binding the formset from
        # ghost-only data would otherwise trip BaseSaleItemFormSet.clean()'s
        # "must add at least one sale item" the moment this page loads,
        # before the cashier has touched anything -- that error belongs
        # only to a genuinely failed Complete Sale submit, never to a
        # plain GET.
        formset_initial = [row for row in draft_forms if row.get('sale_item')]

        if formset_initial:
            form = SaleForms(current_user=request.user)
            restore_post_data = build_restore_formset_data(formset_prefix, formset_initial)

            formset = SaleItemFormSet(
                data=restore_post_data,
                instance=sale_instance,
                prefix=formset_prefix,
            )
            sale_draft = tab_draft
        else:
            if tab_draft and tab_draft.get('active'):
                clear_tab_draft(request, active_tab)
            form = SaleForms(current_user=request.user)
            formset = SaleItemFormSet(instance=sale_instance, prefix=formset_prefix)

    # Seeds the Tabulator cart on page load -- resolves each draft row back
    # to its real Product (name/current sell_price), same idea as
    # `delivery_items_initial` for the Deliveries items grid. Only the
    # draft-restore path ever has rows here; a fresh sale starts with an
    # empty cart, ready for scanning.
    sale_items_initial = []
    if formset_initial:
        product_ids = [row['sale_item'] for row in formset_initial if row.get('sale_item')]
        products_by_id = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)}
        for row in formset_initial:
            if row.get('DELETE'):
                continue
            product = products_by_id.get(int(row['sale_item'])) if row.get('sale_item') else None
            quantity = float(row.get('sale_quantity') or 1)
            price = float(row.get('price_at_sale') or (product.sell_price if product else 0) or 0)
            sale_items_initial.append({
                'sale_item': product.pk if product else '',
                'product_name': product.name if product else row.get('product_name', ''),
                'unit_type': product.unit_type if product else '',
                'sale_quantity': quantity,
                'price_at_sale': price,
                'total_price_row': float(row.get('total_price_row') or quantity * price),
            })

    context = {
        'form': form,
        'formset': formset,
        'formset_prefix': formset_prefix,
        'products_data': products_data,
        'barcodes_data': barcodes_data,
        'categories_data': categories_data,
        'pos_pins_data': pos_pins_data,
        'sale_items_initial': sale_items_initial,
        'sale_draft': sale_draft,
        'sale_formset_initial_count': len(formset_initial) if formset_initial else 0,
        'active_tab': active_tab,
        'sale_tabs_summary': tabs_summary(request),
        'last_change': get_last_change(request, active_tab),
    }
    return render(request, 'sales/sale_add.html', context)


class SalesDetailView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    permission_required = 'sales.view_saleattributes'
    model = SaleAttributes
    template_name = 'sales/sale_details.html'
    context_object_name = 'sale'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['sold_items'] = self.object.items.select_related('sale_item').all()
        return context


class SalesListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'sales.view_saleattributes'
    model = SaleAttributes
    template_name = 'sales/sales_list.html'
    context_object_name = 'sales'


class SalesDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'sales.delete_saleattributes'
    model = SaleAttributes
    template_name = 'sales/sale_confirm_delete.html'
    context_object_name = 'sale'
    success_url = reverse_lazy('sales_list')


@login_required
@require_POST
def sales_draft_save(request):
    payload = json.loads(request.body.decode('utf-8'))
    formset_data = payload.get('formset_data', {})
    active_tab = get_active_tab(request)
    set_tab_draft(request, active_tab, {'formset_data': formset_data, 'active': True})
    # A draft save only ever fires once the cart has items in it again --
    # the cashier has started ringing up the next customer, so the
    # previous sale's Change no longer belongs on screen.
    clear_last_change(request, active_tab)
    return JsonResponse({'status': 'ok'})


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
@require_POST
def switch_sale_tab(request):
    """Switches which of the 3 basket tabs is active -- the JS reloads
    sale_add right after, which then loads whichever draft (or empty cart)
    lives on that tab."""
    payload = json.loads(request.body.decode('utf-8'))
    tab = payload.get('tab')
    if tab not in TAB_IDS:
        return JsonResponse({'error': 'Invalid tab.'}, status=400)
    set_active_tab(request, tab)
    return JsonResponse({'status': 'ok'})


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
def cancel_sale(request):
    """The sales screen's own "Cancel" -- clears just the active tab's
    draft and stays on a fresh sale_add, instead of the generic
    clear_cashier_operation (shared by deliveries/write-off/scrap) which
    would jump away to the sales list."""
    clear_tab_draft(request, get_active_tab(request))
    return redirect('sale_add')


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
@require_POST
def pos_pin_add(request):
    """Pins a category or product to the fixed shortcut bar on the POS
    screen (edit mode, sale_add.html). Only items flagged `show_on_pos`
    can be pinned -- the picker that calls this only ever offers those,
    but it's re-checked here too since this is a plain POST endpoint."""
    payload = json.loads(request.body.decode('utf-8'))
    pin_type = payload.get('type')
    target_id = payload.get('target_id')
    next_position = (PosPin.objects.aggregate(Max('position'))['position__max'] or 0) + 1

    if pin_type == 'category':
        category = get_object_or_404(Category, pk=target_id, show_on_pos=True)
        PosPin.objects.create(category=category, position=next_position)
    elif pin_type == 'product':
        product = get_object_or_404(Product, pk=target_id, show_on_pos=True)
        PosPin.objects.create(product=product, position=next_position)
    else:
        return JsonResponse({'error': 'Invalid type'}, status=400)

    return JsonResponse({'status': 'ok'})


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
@require_POST
def pos_pin_remove(request, pk):
    PosPin.objects.filter(pk=pk).delete()
    return JsonResponse({'status': 'ok'})


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
@require_POST
def log_removed_sale_item(request):
    """Records cart lines removed before a sale is completed -- either one
    line ("Delete line") or the whole in-progress cart ("Void sale"), both
    reachable from sale_add.html. The cart is only session draft state at
    this point (no SaleAttributes row exists yet), so this is the only
    record of the removal short of combing through session history."""
    payload = json.loads(request.body.decode('utf-8'))
    action = payload.get('action')
    if action not in dict(SaleItemVoidLog.ACTION_CHOICES):
        return JsonResponse({'error': 'Invalid action.'}, status=400)

    items = payload.get('items', [])
    product_ids = [item['product_id'] for item in items if item.get('product_id')]
    products_by_id = {p.pk: p for p in Product.objects.filter(pk__in=product_ids)}

    logs = [
        SaleItemVoidLog(
            employee=request.user,
            product=products_by_id.get(int(item['product_id'])) if item.get('product_id') else None,
            quantity=item.get('quantity') or 0,
            unit_price=item.get('unit_price') or None,
            total_price=item.get('total_price') or None,
            action=action,
        )
        for item in items
    ]
    SaleItemVoidLog.objects.bulk_create(logs)
    return JsonResponse({'status': 'ok', 'count': len(logs)})