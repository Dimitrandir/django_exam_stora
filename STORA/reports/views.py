from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import urlencode
from django.views import View
from django.views.generic import DeleteView, DetailView, ListView

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.deliveries.models import DeliveryAttributes, DeliveryItems
from STORA.products.models import Product
from STORA.reports.ai_service import AIReportsNotConfigured, rerun_stored_query, rows_to_dicts, run_ai_report
from STORA.reports.forms import ExpiringPeriodForm, ReportPeriodForm, StockAsOfDateForm
from STORA.reports.models import AIReport
from STORA.reports.services import stock_as_of
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
    """Just the period picker plus links to every report, grouped by
    category (Products / Sales / Stock Movements) -- the old aggregate
    cards and low-stock table were removed per the user's redesign request,
    not folded into another report. The chosen period is carried into the
    links below for whichever reports accept one (dashboard.html appends
    it to the querystring itself), so picking a period once here saves
    re-picking it on each report."""

    template_name = 'reports/dashboard.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)
        period_qs = urlencode({'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()})

        context = {
            'form': form,
            'start_date': start_date,
            'end_date': end_date,
            'sales_report_url': f"{reverse('sales_report')}?{period_qs}",
            'sales_quantity_report_url': f"{reverse('sales_quantity_report')}?{period_qs}",
            'deliveries_report_url': f"{reverse('deliveries_report')}?{period_qs}",
            'can_view_ai_reports': request.user.has_perm('reports.view_aireport'),
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
                'comment': delivery.comment or '',
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


class StockAsOfDateReportView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """"Наличност към дата" -- what each product's stock reportedly was at
    the end of a chosen day. See reports/services.py::stock_as_of for how
    that's reconstructed (there's no day-by-day snapshot to just read)."""

    permission_required = 'products.view_product'
    template_name = 'reports/stock_as_of_report.html'

    def get(self, request, *args, **kwargs):
        default_date = timezone.localdate()
        form = StockAsOfDateForm(request.GET or None, initial={'as_of_date': default_date})

        if form.is_valid():
            as_of_date = form.cleaned_data['as_of_date']
        else:
            as_of_date = default_date

        rows = stock_as_of(as_of_date)
        stock_data = [
            {
                'code': row['product'].internal_code,
                'name': row['product'].name,
                'category': row['product'].category.name if row['product'].category else '',
                'unit_type': row['product'].get_unit_type_display(),
                'quantity_as_of': float(row['quantity_as_of']),
                'current_quantity': float(row['current_quantity']),
            }
            for row in rows
        ]

        context = {
            'form': form,
            'as_of_date': as_of_date,
            'stock_data': stock_data,
        }
        return render(request, self.template_name, context)


class ExpiringProductsReportView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """"Изтичащи срокове" -- delivered batches with a known expiry date,
    soonest first, within a chosen look-ahead window (already-expired ones
    always included). Batch-level, same as the scrap "pick a batch"
    picker (see deliveries/views.py::BatchSearchView) -- the system
    doesn't track remaining quantity per batch (only the product's total),
    so this can't tell how much of a specific old batch is actually still
    on the shelf, only that the batch existed and when it expires."""

    permission_required = 'deliveries.view_deliveryattributes'
    template_name = 'reports/expiring_report.html'
    DEFAULT_DAYS_AHEAD = 30

    def get(self, request, *args, **kwargs):
        form = ExpiringPeriodForm(request.GET or None, initial={'days_ahead': self.DEFAULT_DAYS_AHEAD})

        if form.is_valid():
            days_ahead = form.cleaned_data['days_ahead']
        else:
            days_ahead = self.DEFAULT_DAYS_AHEAD

        today = timezone.localdate()
        cutoff = today + timedelta(days=days_ahead)

        batches = DeliveryItems.objects.filter(
            delivery__movement_type=DeliveryAttributes.MOVEMENT_DELIVERY,
            expiry_date__isnull=False,
            expiry_date__lte=cutoff,
        ).select_related('delivery_item', 'delivery_item__category').order_by('expiry_date')

        batches_data = [
            {
                'code': batch.delivery_item.internal_code,
                'name': batch.delivery_item.name,
                'category': batch.delivery_item.category.name if batch.delivery_item.category else '',
                'quantity': float(batch.delivery_quantity),
                'expiry_date': batch.expiry_date.isoformat(),
                'days_left': (batch.expiry_date - today).days,
                'view_url': reverse('delivery_details', args=[batch.delivery_id]),
            }
            for batch in batches
        ]

        context = {
            'form': form,
            'days_ahead': days_ahead,
            'batches_data': batches_data,
        }
        return render(request, self.template_name, context)


class SalesQuantityReportView(StaffPermissionRequiredMixin, ReportsBaseView):
    """"Продажби за период (количество)" -- how much of each product sold
    in a period, its group, and its delivery/sell price. Manager/Warehouse
    only, same reasoning as DeliveriesReportView above: this exposes
    delivery_price (what the shop pays), not just sales activity."""

    permission_required = 'deliveries.add_deliveryattributes'
    template_name = 'reports/sales_quantity_report.html'

    def get(self, request, *args, **kwargs):
        form, start_date, end_date = self.get_period(request)

        totals_by_product = {
            row['sale_item_id']: row
            for row in (
                SaleItems.objects
                .filter(sale__time_of_sale__date__range=(start_date, end_date))
                .values('sale_item_id')
                .annotate(total_qty=Sum('sale_quantity'), total_amount=Sum('total_price_row'))
            )
        }

        products = Product.objects.filter(pk__in=totals_by_product.keys()).select_related('category')

        sales_quantity_data = [
            {
                'code': product.internal_code,
                'name': product.name,
                'category': product.category.name if product.category else '',
                'quantity_sold': float(totals_by_product[product.pk]['total_qty'] or 0),
                'delivery_price': float(product.delivery_price or 0),
                'sell_price': float(product.sell_price),
                'total_amount': float(totals_by_product[product.pk]['total_amount'] or 0),
            }
            for product in products
        ]

        context = {
            'form': form,
            'start_date': start_date,
            'end_date': end_date,
            'sales_quantity_data': sales_quantity_data,
        }
        return render(request, self.template_name, context)


class AIReportListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    """"AI mode" -- saved natural-language reports. Manager-only: unlike
    every other report here, this one can read literally any table (see
    CLAUDE.md/ai_service.py -- deliberately not restricted in what it can
    query), so it gets the narrowest audience rather than the
    Manager/Warehouse split used for merely cost-sensitive reports."""

    permission_required = 'reports.view_aireport'
    model = AIReport
    template_name = 'reports/ai_report_list.html'
    context_object_name = 'ai_reports'


class AIReportCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    permission_required = 'reports.add_aireport'
    template_name = 'reports/ai_report_create.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {})

    def post(self, request, *args, **kwargs):
        prompt = request.POST.get('prompt', '').strip()
        if not prompt:
            return render(request, self.template_name, {'error': 'Type a question first.'})

        try:
            result = run_ai_report(prompt)
        except AIReportsNotConfigured:
            return render(request, self.template_name, {
                'error': 'AI mode isn’t set up yet -- ANTHROPIC_API_KEY is missing from .env.',
                'prompt': prompt,
            })
        except Exception as exc:
            return render(request, self.template_name, {
                'error': f'Something went wrong talking to the AI: {exc}',
                'prompt': prompt,
            })

        report = AIReport.objects.create(
            prompt=prompt,
            generated_sql=result['sql'] or '',
            last_answer=result['answer'],
            created_by=request.user,
            last_regenerated_at=timezone.now(),
        )
        return redirect('ai_report_detail', pk=report.pk)


class AIReportDetailView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    """Re-runs the report's stored SQL fresh on every view (see
    AIReport's docstring) -- the numbers shown are never a stale snapshot,
    only the SQL itself and the narrative answer stay put until someone
    explicitly hits Regenerate."""

    permission_required = 'reports.view_aireport'
    model = AIReport
    template_name = 'reports/ai_report_detail.html'
    context_object_name = 'ai_report'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        columns, rows, error = rerun_stored_query(self.object.generated_sql)
        context['columns'] = columns
        context['table_data'] = rows_to_dicts(columns, rows)
        context['rerun_error'] = error
        return context


class AIReportRegenerateView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    permission_required = 'reports.add_aireport'

    def post(self, request, *args, **kwargs):
        report = get_object_or_404(AIReport, pk=kwargs['pk'])
        try:
            result = run_ai_report(report.prompt)
        except AIReportsNotConfigured:
            messages.error(request, 'AI mode isn’t set up yet -- ANTHROPIC_API_KEY is missing from .env.')
            return redirect('ai_report_detail', pk=report.pk)
        except Exception as exc:
            messages.error(request, f'Regenerate failed: {exc}')
            return redirect('ai_report_detail', pk=report.pk)

        report.generated_sql = result['sql'] or ''
        report.last_answer = result['answer']
        report.last_regenerated_at = timezone.now()
        report.save(update_fields=['generated_sql', 'last_answer', 'last_regenerated_at'])
        return redirect('ai_report_detail', pk=report.pk)


class AIReportDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'reports.delete_aireport'
    model = AIReport
    context_object_name = 'ai_report'
    template_name = 'reports/ai_report_confirm_delete.html'
    success_url = reverse_lazy('ai_report_list')