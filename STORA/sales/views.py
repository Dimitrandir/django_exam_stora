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
from STORA.core.session_service import (
    clear_cashier_operation_state,
    extract_formset_state,
    get_cashier_operation_state,
    set_cashier_operation_state,
)
from STORA.core.utils import build_cashier_operation_state, build_restore_formset_data, dispatch_task
from STORA.products.models import Product, Barcode, Category
from STORA.sales.forms import SaleForms, SaleItemFormSet
from STORA.sales.models import SaleAttributes, SaleItems, PosPin

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
    state = get_cashier_operation_state(request)
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

            clear_cashier_operation_state(request)
            return redirect('sales_list')

        extracted_state = extract_formset_state(request.POST, formset_prefix)
        sale_draft = build_cashier_operation_state(
            operation_type='sale',
            path=request.path,
            data={
                'cashier_id': request.user.pk,
                'cashier_username': request.user.username,
            },
            formset_data=extracted_state,
            active=True,
        )
        set_cashier_operation_state(request, sale_draft)

        form = SaleForms(request.POST, current_user=request.user)
        formset_initial = extracted_state.get('forms', [])
        formset = SaleItemFormSet(
            request.POST,
            instance=sale_instance,
            prefix=formset_prefix,
            initial=formset_initial,
        )
    else:
        if state and state.get('type') == 'sale' and state.get('active'):
            form = SaleForms(current_user=request.user)
            formset_initial = state.get('formset_data', {}).get('forms', []) or [{}]
            restore_post_data = build_restore_formset_data(formset_prefix, formset_initial)

            formset = SaleItemFormSet(
                data=restore_post_data,
                instance=sale_instance,
                prefix=formset_prefix,
            )
            sale_draft = state
        else:
            form = SaleForms(current_user=request.user)
            formset = SaleItemFormSet(instance=sale_instance, prefix=formset_prefix)

    context = {
        'form': form,
        'formset': formset,
        'formset_prefix': formset_prefix,
        'products_data': products_data,
        'barcodes_data': barcodes_data,
        'categories_data': categories_data,
        'pos_pins_data': pos_pins_data,
        'sale_draft': sale_draft,
        'sale_formset_initial_count': len(formset_initial) if formset_initial else 0,
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
    form_data = payload.get('form_data', {})
    formset_data = payload.get('formset_data', {})

    draft = build_cashier_operation_state(
        operation_type='sale',
        path='/sales/add/',
        data=form_data,
        formset_data=formset_data,
        active=True,
    )
    set_cashier_operation_state(request, draft)
    return JsonResponse({'status': 'ok'})


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