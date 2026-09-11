from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType
from STORA.products.models import Product, Suppliers, TaxGroup


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

    def test_summary_totals(self):
        response = self._get()
        self.assertEqual(response.context['total_deliveries_count'], 1)
        self.assertEqual(response.context['total_deliveries_amount'], Decimal('29.00'))

    def test_write_offs_do_not_appear_in_deliveries_report(self):
        # Write-offs share DeliveryAttributes with deliveries now -- without
        # an explicit movement_type filter they'd leak into this report
        # (and document_type.name would crash, since write-offs have none).
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

    def test_scrap_does_not_appear_in_deliveries_report(self):
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
