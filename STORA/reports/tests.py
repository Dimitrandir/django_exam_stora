from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType
from STORA.products.models import Product, Suppliers, TaxGroup
from STORA.sales.models import RefundAttributes, RefundItems, SaleAttributes, SaleItems


User = get_user_model()


class DeliveriesReportViewTests(TestCase):
    """DeliveriesReportView backs the Tabulator-driven deliveries overview
    (period + supplier filter, per-delivery totals with/without VAT). Zero
    coverage before this -- per CLAUDE.md, tests go in before refactoring a
    reports view, not just after."""

    def setUp(self):
        self.manager = User.objects.create_user(username='manager20', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)

        self.supplier_a = Suppliers.objects.create(name='Supplier A', bulstat='111111111')
        self.supplier_b = Suppliers.objects.create(name='Supplier B', bulstat='222222222')
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.tax_group_20, _ = TaxGroup.objects.get_or_create(name='Standard 20', defaults={'rate': Decimal('20')})

        self.product_taxed = Product.objects.create(
            internal_code='RPT001', name='Taxed Product', delivery_price=Decimal('12.00'),
            sell_price=Decimal('20.00'), quantity=0, tax_group=self.tax_group_20,
        )
        self.product_untaxed = Product.objects.create(
            internal_code='RPT002', name='Untaxed Product', delivery_price=Decimal('5.00'),
            sell_price=Decimal('8.00'), quantity=0,
        )

        today = timezone.localdate()

        self.in_range_delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, supplier=self.supplier_a, document_type=self.invoice_type,
            document_number='INV-200', document_date=today,
            time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=self.in_range_delivery, delivery_item=self.product_taxed,
            delivery_quantity=Decimal('2.000'), price_at_delivery=Decimal('12.00'),
        )
        DeliveryItems.objects.create(
            delivery=self.in_range_delivery, delivery_item=self.product_untaxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('5.00'),
        )

        self.out_of_range_delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, supplier=self.supplier_b, document_type=self.invoice_type,
            document_number='INV-201', document_date=today - timezone.timedelta(days=30),
            time_of_delivery=timezone.now() - timezone.timedelta(days=30),
        )
        DeliveryItems.objects.create(
            delivery=self.out_of_range_delivery, delivery_item=self.product_untaxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('5.00'),
        )

    def _get(self, **params):
        today = timezone.localdate()
        data = {
            'start_date': (today - timezone.timedelta(days=7)).isoformat(),
            'end_date': today.isoformat(),
        }
        data.update(params)
        return self.client.get(reverse('deliveries_report'), data)

    def test_requires_login(self):
        self.client.logout()
        response = self._get()
        self.assertEqual(response.status_code, 302)

    def test_filters_out_deliveries_outside_period(self):
        response = self._get()
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertIn(self.in_range_delivery.pk, ids)
        self.assertNotIn(self.out_of_range_delivery.pk, ids)

    def test_filters_by_supplier(self):
        # Regression check: the supplier field has existed on the filter
        # form/template since before this session but was never actually
        # applied to the queryset -- picking a supplier silently did nothing.
        response = self._get(supplier=self.supplier_b.pk)
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertNotIn(self.in_range_delivery.pk, ids)

        response = self._get(
            supplier=self.supplier_a.pk,
            start_date=(timezone.localdate() - timezone.timedelta(days=1)).isoformat(),
        )
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertIn(self.in_range_delivery.pk, ids)

    def test_deliveries_data_shape(self):
        response = self._get()
        row = next(d for d in response.context['deliveries_data'] if d['id'] == self.in_range_delivery.pk)
        self.assertEqual(row['supplier'], 'Supplier A')
        self.assertEqual(row['document_type'], 'Invoice')
        self.assertEqual(row['document_number'], 'INV-200')
        self.assertEqual(row['document_date'], timezone.localdate().isoformat())
        self.assertIn('time_of_delivery', row)
        self.assertIn('view_url', row)

    def test_total_amount_without_vat_computed_per_item_tax_group(self):
        response = self._get()
        row = next(d for d in response.context['deliveries_data'] if d['id'] == self.in_range_delivery.pk)
        # 2 x 12.00 (20% VAT) -> 2 x 10.00 = 20.00 without VAT
        # 1 x 5.00 (no tax group) -> 5.00 as-is
        # total without VAT = 25.00; total with VAT (from DeliveryItems.save()) = 2*12 + 1*5 = 29.00
        self.assertEqual(row['total_amount'], 29.0)
        self.assertEqual(row['total_amount_without_vat'], 25.0)

    def test_write_offs_do_not_appear_in_deliveries_report_by_default(self):
        # Write-offs share DeliveryAttributes with deliveries -- without an
        # explicit movement_type filter they'd leak into this report (and
        # document_type.name would crash, since write-offs have none).
        write_off = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.manager, supplier=self.supplier_a,
            document_date=timezone.localdate(), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=write_off, delivery_item=self.product_taxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('12.00'),
        )
        response = self._get()
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertNotIn(write_off.pk, ids)

    def test_scrap_does_not_appear_in_deliveries_report_by_default(self):
        scrap = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_SCRAP,
            receiver=self.manager, document_date=timezone.localdate(), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=scrap, delivery_item=self.product_taxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('12.00'),
        )
        response = self._get()
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertNotIn(scrap.pk, ids)

    def test_movement_type_write_off_shows_only_write_offs(self):
        # Write-off has no document_type/supplier -- both None. The row
        # must still render (not crash) with blank strings for them.
        write_off = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.manager, document_date=timezone.localdate(), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=write_off, delivery_item=self.product_taxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('12.00'),
        )
        response = self._get(movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF)
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertIn(write_off.pk, ids)
        self.assertNotIn(self.in_range_delivery.pk, ids)
        row = next(d for d in response.context['deliveries_data'] if d['id'] == write_off.pk)
        self.assertEqual(row['document_type'], '')
        self.assertEqual(row['supplier'], '')

    def test_movement_type_scrap_shows_only_scrap(self):
        scrap = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_SCRAP,
            receiver=self.manager, document_date=timezone.localdate(), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=scrap, delivery_item=self.product_taxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('12.00'),
        )
        response = self._get(movement_type=DeliveryAttributes.MOVEMENT_SCRAP)
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertIn(scrap.pk, ids)
        self.assertNotIn(self.in_range_delivery.pk, ids)

    def test_invalid_movement_type_falls_back_to_delivery(self):
        response = self._get(movement_type='NOT_A_REAL_TYPE')
        self.assertEqual(response.context['movement_type'], DeliveryAttributes.MOVEMENT_DELIVERY)
        ids = [d['id'] for d in response.context['deliveries_data']]
        self.assertIn(self.in_range_delivery.pk, ids)


class SalesReportViewTests(TestCase):
    """Replaces the old plain sales_list.html as the app's "browse all
    sales" screen (Tabulator-driven, period filter only -- the category
    filter was removed as unneeded). Covers the two things unique to this
    view: item_qty summed across a sale's lines, and refund_status derived
    by comparing sold vs. refunded quantity."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='report-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)

        self.product = Product.objects.create(
            internal_code='SR000001', name='Report Product', sell_price=Decimal('5.00'), quantity=10,
        )

        self.sale = SaleAttributes.objects.create(cashier=self.cashier)
        self.item = SaleItems.objects.create(
            sale=self.sale, sale_item=self.product, sale_quantity=Decimal('3.000'), price_at_sale=Decimal('5.00'),
        )

    def _get(self, **params):
        today = timezone.localdate()
        data = {
            'start_date': (today - timezone.timedelta(days=7)).isoformat(),
            'end_date': today.isoformat(),
        }
        data.update(params)
        return self.client.get(reverse('sales_report'), data)

    def test_requires_login(self):
        self.client.logout()
        response = self._get()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_sale_appears_with_correct_item_qty_and_not_refunded_status(self):
        response = self._get()
        row = next(r for r in response.context['sales_data'] if r['id'] == self.sale.pk)
        self.assertEqual(row['item_qty'], 3.0)
        self.assertEqual(row['total_amount'], 15.0)
        self.assertEqual(row['refund_status'], 'Not')
        self.assertEqual(row['cashier'], str(self.cashier))

    def test_partial_refund_shows_partial_status(self):
        refund = RefundAttributes.objects.create(
            original_sale=self.sale, cashier=self.cashier, reason=RefundAttributes.RETURN_COMPLAINT,
        )
        RefundItems.objects.create(
            refund=refund, original_item=self.item, refund_quantity=Decimal('1.000'), price_at_refund=Decimal('5.00'),
        )
        response = self._get()
        row = next(r for r in response.context['sales_data'] if r['id'] == self.sale.pk)
        self.assertEqual(row['refund_status'], 'Partial')

    def test_full_refund_shows_fully_status(self):
        refund = RefundAttributes.objects.create(
            original_sale=self.sale, cashier=self.cashier, reason=RefundAttributes.RETURN_COMPLAINT,
        )
        RefundItems.objects.create(
            refund=refund, original_item=self.item, refund_quantity=Decimal('3.000'), price_at_refund=Decimal('5.00'),
        )
        response = self._get()
        row = next(r for r in response.context['sales_data'] if r['id'] == self.sale.pk)
        self.assertEqual(row['refund_status'], 'Fully')

    def test_filters_out_sales_outside_period(self):
        old_sale = SaleAttributes.objects.create(cashier=self.cashier)
        old_sale.time_of_sale = timezone.now() - timezone.timedelta(days=30)
        old_sale.save(update_fields=['time_of_sale'])

        response = self._get()
        ids = [r['id'] for r in response.context['sales_data']]
        self.assertNotIn(old_sale.pk, ids)
