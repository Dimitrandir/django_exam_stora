from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.deliveries.models import DeliveryAttributes
from STORA.products.models import Product
from STORA.reports.forms import ReportPeriodForm
from STORA.sales.models import SaleAttributes, SaleItems, RefundItems


class ReportsBaseView(LoginRequiredMixin, View):
    def get_default_period(self):
        end_date = timezone.localdate()
        start_date = end_date - timedelta(days=7)
        return start_date, end_date

    def get_period(self, request):
        default_start, default_end = self.get_default_period()

        form = ReportPeriodForm(
            request.GET or None,
            initial={
                'start_date': default_start,
                'end_date': default_end,
            }
        )

        if form.is_valid():
            start_date = form.cleaned_data['start_date']
            end_date = form.cleaned_data['end_date']
        else:
            start_date = default_start
            end_date = default_end

        return form, start_date, end_date


class ReportsDashboardView(ReportsBaseView):
    template_name = 'reports/dashboard.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)

        sales = SaleAttributes.objects.filter(
            time_of_sale__date__range=(start_date, end_date)
        )
        deliveries = DeliveryAttributes.objects.filter(
            movement_type=DeliveryAttributes.MOVEMENT_DELIVERY,
            time_of_delivery__date__range=(start_date, end_date)
        )

        total_sales_count = sales.count()
        total_sales_amount = sales.aggregate(total=Sum('total_amount'))['total'] or 0

        total_deliveries_count = deliveries.count()
        total_deliveries_amount = deliveries.aggregate(total=Sum('total_amount'))['total'] or 0

        # A recipe product's own `quantity` is never touched by sales (only
        # its ingredients are decremented -- see CLAUDE.md) -- it just sits
        # at whatever it was left at (usually 0), so it would otherwise
        # show up here forever regardless of real stock, drowning out
        # products whose quantity actually means something.
        low_stock_products = Product.objects.filter(
            quantity__lte=5, is_recipe=False
        ).order_by('quantity')[:10]

        context = {
            'form': form,
            'start_date': start_date,
            'end_date': end_date,
            'total_sales_count': total_sales_count,
            'total_sales_amount': total_sales_amount,
            'total_deliveries_count': total_deliveries_count,
            'total_deliveries_amount': total_deliveries_amount,
            'low_stock_products': low_stock_products,
        }
        return render(request, self.template_name, context)


class SalesReportView(ReportsBaseView):
    """Backs the Tabulator-driven sales overview -- also the app's only
    "browse all sales" screen now (the old plain sales_list.html was
    removed as a redundant, less capable duplicate of this page)."""

    template_name = 'reports/sales_report.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)

        sales = SaleAttributes.objects.filter(
            time_of_sale__date__range=(start_date, end_date)
        ).select_related('cashier')

        sales = list(sales.order_by('-time_of_sale'))
        sale_ids = [sale.pk for sale in sales]

        # Two separate grouped queries, not one .annotate() summing both
        # `items__sale_quantity` and `refunds__items__refund_quantity` at
        # once -- combining two different reverse-FK paths in a single
        # annotate() joins items x refund-items and inflates both sums.
        # Same "aggregate separately, merge in Python" pattern as
        # recent_qty_by_product in sales/views.py.
        item_qty_by_sale = dict(
            SaleItems.objects.filter(sale_id__in=sale_ids)
            .values('sale_id').annotate(total=Sum('sale_quantity')).values_list('sale_id', 'total')
        )
        refunded_qty_by_sale = dict(
            RefundItems.objects.filter(refund__original_sale_id__in=sale_ids)
            .values('refund__original_sale_id').annotate(total=Sum('refund_quantity'))
            .values_list('refund__original_sale_id', 'total')
        )

        sales_data = []
        for sale in sales:
            item_qty = item_qty_by_sale.get(sale.pk) or Decimal('0')
            refunded_qty = refunded_qty_by_sale.get(sale.pk) or Decimal('0')
            if refunded_qty <= 0:
                refund_status = 'Not'
            elif item_qty and refunded_qty >= item_qty:
                refund_status = 'Fully'
            else:
                refund_status = 'Partial'

            local_time = timezone.localtime(sale.time_of_sale)
            sales_data.append({
                'id': sale.pk,
                'date': local_time.strftime('%Y-%m-%d'),
                'time': local_time.strftime('%H:%M'),
                'cashier': str(sale.cashier),
                'item_qty': float(item_qty),
                'total_amount': float(sale.total_amount or 0),
                'refund_status': refund_status,
                'view_url': reverse('sale_details', args=[sale.pk]),
            })

        context = {
            'form': form,
            'start_date': start_date,
            'end_date': end_date,
            'sales_data': sales_data,
        }
        return render(request, self.template_name, context)


class DeliveriesReportView(StaffPermissionRequiredMixin, ReportsBaseView):
    """One shared "Stock Movements" report for all three DeliveryAttributes
    movement types (Delivery/Write-off/Scrap) -- picked via `movement_type`
    in the querystring (defaults to Delivery). Kept as one view/template
    rather than three, since they're the same underlying model and table
    shape; only which rows match differs.

    Manager/Warehouse only -- this report shows supplier names and delivery
    COSTS (what the shop pays), unlike the individual delivery detail page.
    Reuses `deliveries.add_deliveryattributes` rather than
    `view_deliveryattributes`: Cashiers already hold view_deliveryattributes
    (so they can check whether stock has arrived on a specific delivery,
    see accounts/signals.py), so gating on that wouldn't actually exclude
    them here -- add_deliveryattributes is Warehouse+Manager only, which is
    exactly the split wanted.
    """

    permission_required = 'deliveries.add_deliveryattributes'
    template_name = 'reports/deliveries_report.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)

        movement_type = request.GET.get('movement_type', DeliveryAttributes.MOVEMENT_DELIVERY)
        if movement_type not in dict(DeliveryAttributes.MOVEMENT_TYPE_CHOICES):
            movement_type = DeliveryAttributes.MOVEMENT_DELIVERY

        deliveries = DeliveryAttributes.objects.filter(
            movement_type=movement_type,
            time_of_delivery__date__range=(start_date, end_date)
        ).select_related('supplier', 'document_type').prefetch_related(
            'items__delivery_item__tax_group'
        ).order_by('-time_of_delivery')

        # The `supplier` field has been on this filter form/template for a
        # while but was never actually applied to the queryset -- picking
        # one silently did nothing.
        selected_supplier = form.cleaned_data.get('supplier') if form.is_valid() else None
        if selected_supplier:
            deliveries = deliveries.filter(supplier=selected_supplier)

        deliveries_data = [
            {
                'id': delivery.pk,
                'time_of_delivery': timezone.localtime(delivery.time_of_delivery).strftime('%Y-%m-%d %H:%M'),
                # document_type/supplier are both None for Write-off/Scrap
                # (no incoming document to classify, no source supplier) --
                # this view used to assume Delivery-only and crashed on
                # either being unset.
                'document_type': delivery.document_type.name if delivery.document_type else '',
                'document_number': delivery.document_number or '',
                'document_date': delivery.document_date.isoformat() if delivery.document_date else '',
                'supplier': delivery.supplier.name if delivery.supplier else '',
                'total_amount': float(delivery.total_amount or 0),
                'total_amount_without_vat': float(self._total_without_vat(delivery)),
                'view_url': reverse('delivery_details', args=[delivery.pk]),
            }
            for delivery in deliveries
        ]

        context = {
            'form': form,
            'start_date': start_date,
            'end_date': end_date,
            'deliveries_data': deliveries_data,
            'movement_type': movement_type,
            'movement_type_choices': DeliveryAttributes.MOVEMENT_TYPE_CHOICES,
            'selected_supplier_name': selected_supplier.name if selected_supplier else '',
        }
        return render(request, self.template_name, context)

    @staticmethod
    def _total_without_vat(delivery):
        """Sums each item's price with its OWN product's tax_group.rate
        stripped out -- a delivery can mix taxed and untaxed products (or
        products in different tax groups), so there's no single rate to
        divide the delivery's total by. Products with no tax_group are
        treated as already VAT-exclusive (0%), same convention as the
        product detail page and the delivery items grid."""
        total = Decimal('0.00')
        for item in delivery.items.all():
            product = item.delivery_item
            price = item.price_at_delivery or Decimal('0.00')
            if product.tax_group:
                price = price / (Decimal('1') + product.tax_group.rate / Decimal('100'))
            total += (item.delivery_quantity or Decimal('0')) * price
        return total.quantize(Decimal('0.01'))