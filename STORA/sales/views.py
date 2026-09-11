from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse_lazy
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
from STORA.sales.models import SaleAttributes


@login_required
@permission_required('sales.add_saleattributes', raise_exception=True)
def sales_add(request):
    # category_id feeds the category/subcategory quick-pick panel (Фаза 2) --
    # it groups these same products by category client-side, no extra
    # requests per click.
    products_data = list(Product.objects.values('id', 'internal_code', 'name', 'sell_price', 'unit_type', 'category_id'))
    barcodes_data = list(Barcode.objects.values('code', 'product_id', 'is_scale_code'))
    categories_data = list(Category.objects.values('id', 'name', 'parent_id'))

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