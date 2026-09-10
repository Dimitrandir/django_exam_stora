from decimal import Decimal

from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.views.decorators.http import require_POST

import json

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.core.session_service import (
    clear_cashier_operation_state,
    extract_formset_state,
    get_cashier_operation_state,
    set_cashier_operation_state,
)
from STORA.core.utils import build_cashier_operation_state, build_restore_formset_data
from STORA.deliveries.forms import DeliveryForms, DeliveryItemFormSet, DocumentTypeForm
from STORA.deliveries.models import DeliveryAttributes, DocumentType
from STORA.products.models import Product, Barcode, Suppliers


def _items_initial_from_rows(rows):
    """Shapes a list of per-row dicts (from extract_formset_state, a draft's
    stored formset_data, or built straight from DeliveryItems below) into
    what the Tabulator grid in `_delivery_items_table.html` expects. The JS
    resolves `delivery_item` against the already-loaded `products_data` for
    display (code/name/unit_type) -- this only needs to carry the bare
    model values."""
    return [
        {
            'id': row.get('id') or '',
            'delivery_item': row.get('delivery_item') or '',
            'delivery_quantity': row.get('delivery_quantity') or '',
            'price_at_delivery': row.get('price_at_delivery') or '',
            'total_price_row': row.get('total_price_row') or '',
            'expiry_date': row.get('expiry_date') or '',
        }
        for row in rows
        if row.get('delivery_item')
    ]


def _items_initial_from_instance(delivery):
    return _items_initial_from_rows([
        {
            'id': item.pk,
            'delivery_item': item.delivery_item_id,
            'delivery_quantity': item.delivery_quantity,
            'price_at_delivery': item.price_at_delivery,
            'total_price_row': item.total_price_row,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else '',
        }
        for item in delivery.items.all()
    ])


@login_required
@permission_required('deliveries.add_deliveryattributes', raise_exception=True)
def deliveries_add(request):
    products_data = list(Product.objects.values('id', 'internal_code', 'name', 'delivery_price', 'sell_price', 'unit_type', 'tax_group__rate'))
    barcodes_data = list(Barcode.objects.values('code', 'product_id'))

    formset_prefix = 'items'
    state = get_cashier_operation_state(request)
    delivery_draft = None
    formset_initial = []
    delivery_items_initial = []
    selected_supplier_name = ''

    if request.method == "POST":
        form = DeliveryForms(request.POST, current_user=request.user)
        formset = DeliveryItemFormSet(request.POST, prefix=formset_prefix)

        if form.is_valid() and formset.is_valid():
            delivery = form.save(commit=False)
            delivery.receiver = request.user
            delivery.save()

            formset.instance = delivery
            formset.save()

            clear_cashier_operation_state(request)
            return redirect('deliveries_list')

        extracted_state = extract_formset_state(request.POST, formset_prefix)
        delivery_items_initial = _items_initial_from_rows(extracted_state.get('forms', []))
        posted_supplier_id = request.POST.get('supplier') or None
        selected_supplier = Suppliers.objects.filter(pk=posted_supplier_id).first() if posted_supplier_id else None
        selected_supplier_name = selected_supplier.name if selected_supplier else ''
        delivery_draft = build_cashier_operation_state(
            operation_type='delivery',
            path=request.path,
            data={
                'receiver': request.POST.get('receiver', ''),
                'supplier': request.POST.get('supplier', ''),
                'time_of_delivery': request.POST.get('time_of_delivery', ''),
                'document_type': request.POST.get('document_type', ''),
                'document_number': request.POST.get('document_number', ''),
                'document_date': request.POST.get('document_date', ''),
            },
            formset_data=extracted_state,
            active=True,
        )
        set_cashier_operation_state(request, delivery_draft)

        form = DeliveryForms(request.POST, current_user=request.user)
        formset_initial = extracted_state.get('forms', [])
        formset = DeliveryItemFormSet(
            request.POST,
            prefix=formset_prefix,
            initial=formset_initial,
        )
    else:
        if state and state.get('type') == 'delivery' and state.get('active'):
            form = DeliveryForms(initial=state.get('data', {}), current_user=request.user)
            formset_initial = state.get('formset_data', {}).get('forms', []) or [{}]
            restore_post_data = build_restore_formset_data(formset_prefix, formset_initial)
            delivery_items_initial = _items_initial_from_rows(formset_initial)
            draft_supplier_id = state.get('data', {}).get('supplier') or None
            selected_supplier = Suppliers.objects.filter(pk=draft_supplier_id).first() if draft_supplier_id else None
            selected_supplier_name = selected_supplier.name if selected_supplier else ''

            formset = DeliveryItemFormSet(
                data=restore_post_data,
                prefix=formset_prefix,
            )
            delivery_draft = state
        else:
            form = DeliveryForms(current_user=request.user)
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
    }
    return render(request, 'deliveries/delivery_add.html', context)


@login_required
@require_POST
def delivery_draft_save(request):
    payload = json.loads(request.body.decode('utf-8'))
    form_data = payload.get('form_data', {})
    formset_data = payload.get('formset_data', {})

    draft = build_cashier_operation_state(
        operation_type='delivery',
        path='/deliveries/add/',
        data=form_data,
        formset_data=formset_data,
        active=True,
    )
    set_cashier_operation_state(request, draft)
    return JsonResponse({'status': 'ok'})


class DeliveryDetailView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    permission_required = 'deliveries.view_deliveryattributes'
    model = DeliveryAttributes
    template_name = 'deliveries/delivery_details.html'
    context_object_name = 'delivery'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = self.object.items.select_related('delivery_item', 'delivery_item__tax_group').all()
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
        }


class DeliveryListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'deliveries.view_deliveryattributes'
    model = DeliveryAttributes
    template_name = 'deliveries/deliveries_list.html'
    context_object_name = 'deliveries'


@login_required
@permission_required('deliveries.change_deliveryattributes', raise_exception=True)
def delivery_edit(request, pk):
    delivery = get_object_or_404(DeliveryAttributes, pk=pk)

    products_data = list(Product.objects.values('id', 'internal_code', 'name', 'delivery_price', 'sell_price', 'unit_type', 'tax_group__rate'))
    barcodes_data = list(Barcode.objects.values('code', 'product_id'))

    formset_prefix = 'items'

    if request.method == "POST":
        form = DeliveryForms(request.POST, instance=delivery)
        formset = DeliveryItemFormSet(request.POST, instance=delivery, prefix=formset_prefix)

        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            return redirect('delivery_details', pk=delivery.pk)

        extracted_state = extract_formset_state(request.POST, formset_prefix)
        delivery_items_initial = _items_initial_from_rows(extracted_state.get('forms', []))
    else:
        form = DeliveryForms(instance=delivery)
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
