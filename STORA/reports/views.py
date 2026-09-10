from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Sum
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from STORA.deliveries.models import DeliveryAttributes
from STORA.products.models import Product
from STORA.reports.forms import ReportPeriodForm
from STORA.sales.models import SaleAttributes


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
            time_of_delivery__date__range=(start_date, end_date)
        )

        total_sales_count = sales.count()
        total_sales_amount = sales.aggregate(total=Sum('total_amount'))['total'] or 0

        total_deliveries_count = deliveries.count()
        total_deliveries_amount = deliveries.aggregate(total=Sum('total_amount'))['total'] or 0

        low_stock_products = Product.objects.filter(quantity__lte=5).order_by('quantity')[:10]

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
    template_name = 'reports/sales_report.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)

        sales = SaleAttributes.objects.filter(
            time_of_sale__date__range=(start_date, end_date)
        )

        selected_category = form.cleaned_data.get('category') if form.is_valid() else None
        if selected_category:
            sales = sales.filter(items__sale_item__category=selected_category).distinct()

        sales = sales.annotate(item_count=Count('items')).order_by('-time_of_sale')
        top_sales = sales.order_by('-total_amount')[:10]

        total_sales_count = sales.count()
        total_sales_amount = sales.aggregate(total=Sum('total_amount'))['total'] or 0

        context = {
            'form': form,
            'start_date': start_date,
            'end_date': end_date,
            'sales': sales,
            'top_sales': top_sales,
            'total_sales_count': total_sales_count,
            'total_sales_amount': total_sales_amount,
        }
        return render(request, self.template_name, context)


class DeliveriesReportView(ReportsBaseView):
    template_name = 'reports/deliveries_report.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)

        deliveries = DeliveryAttributes.objects.filter(
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

        total_deliveries_count = deliveries.count()
        total_deliveries_amount = deliveries.aggregate(total=Sum('total_amount'))['total'] or 0

        deliveries_data = [
            {
                'id': delivery.pk,
                'time_of_delivery': timezone.localtime(delivery.time_of_delivery).strftime('%Y-%m-%d %H:%M'),
                'document_type': delivery.document_type.name,
                'document_number': delivery.document_number or '',
                'document_date': delivery.document_date.isoformat() if delivery.document_date else '',
                'supplier': delivery.supplier.name,
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
            'total_deliveries_count': total_deliveries_count,
            'total_deliveries_amount': total_deliveries_amount,
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