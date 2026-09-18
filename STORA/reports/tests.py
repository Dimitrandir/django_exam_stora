from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType
from STORA.products.models import Category, Product, RecipeIngredient, Suppliers, TaxGroup
from STORA.reports.ai_service import AIReportsNotConfigured
from STORA.reports.models import AIReport
from STORA.revisions.models import RevisionAttributes, RevisionItems
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

    def test_invalid_period_shows_form_errors_instead_of_silently_falling_back(self):
        # start_date after end_date is the one thing ReportPeriodForm
        # actually validates -- the view used to fall back to the default
        # period with zero indication anything was wrong with what was
        # typed.
        response = self._get(
            start_date=timezone.localdate().isoformat(),
            end_date=(timezone.localdate() - timezone.timedelta(days=1)).isoformat(),
        )
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response, 'form-errors')

    def test_cashier_cannot_view_stock_movements_report(self):
        # Shows supplier names and delivery COSTS -- Manager/Warehouse only.
        # Cashier already holds view_deliveryattributes (for the individual
        # delivery detail page), so this deliberately checks against
        # add_deliveryattributes instead -- see DeliveriesReportView.
        cashier = User.objects.create_user(username='report-cashier2', password='pass12345', role=User.CASHIER)
        self.client.force_login(cashier)
        response = self._get()
        self.assertEqual(response.status_code, 403)

    def test_warehouse_can_view_stock_movements_report(self):
        warehouse = User.objects.create_user(username='report-warehouse1', password='pass12345', role=User.WAREHOUSE)
        self.client.force_login(warehouse)
        response = self._get()
        self.assertEqual(response.status_code, 200)

    def test_logged_out_redirected_to_login_not_403(self):
        self.client.logout()
        response = self._get()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)


class ReportsDashboardViewTests(TestCase):
    """Redesigned dashboard: just the period picker plus category-grouped
    links to the other reports (the old aggregate cards and low-stock
    table were removed, not moved elsewhere, per the user's request). The
    chosen period is carried into the links for whichever reports accept
    one, so it's covered here too."""

    def setUp(self):
        self.manager = User.objects.create_user(username='report-manager1', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)

    def _get(self, **params):
        today = timezone.localdate()
        data = {
            'start_date': (today - timezone.timedelta(days=7)).isoformat(),
            'end_date': today.isoformat(),
        }
        data.update(params)
        return self.client.get(reverse('reports_dashboard'), data)

    def test_requires_login(self):
        self.client.logout()
        response = self._get()
        self.assertEqual(response.status_code, 302)

    def test_any_logged_in_role_can_view(self):
        # Deliberately not permission-gated -- this page itself shows no
        # data, only links (some of which are gated on their own view).
        cashier = User.objects.create_user(username='report-cashier3', password='pass12345', role=User.CASHIER)
        self.client.force_login(cashier)
        response = self._get()
        self.assertEqual(response.status_code, 200)

    def test_invalid_period_shows_form_errors(self):
        response = self._get(
            start_date=timezone.localdate().isoformat(),
            end_date=(timezone.localdate() - timezone.timedelta(days=1)).isoformat(),
        )
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response, 'form-errors')

    def test_chosen_period_is_carried_into_period_based_report_links(self):
        start = timezone.localdate() - timezone.timedelta(days=14)
        end = timezone.localdate() - timezone.timedelta(days=10)
        response = self._get(start_date=start.isoformat(), end_date=end.isoformat())

        expected_qs = f'start_date={start.isoformat()}&end_date={end.isoformat()}'
        self.assertIn(expected_qs, response.context['sales_report_url'])
        self.assertIn(expected_qs, response.context['sales_quantity_report_url'])
        self.assertIn(expected_qs, response.context['deliveries_report_url'])


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

    def test_invalid_period_shows_form_errors_instead_of_silently_falling_back(self):
        response = self._get(
            start_date=timezone.localdate().isoformat(),
            end_date=(timezone.localdate() - timezone.timedelta(days=1)).isoformat(),
        )
        self.assertTrue(response.context['form'].errors)
        self.assertContains(response, 'form-errors')


class StockAsOfDateReportViewTests(TestCase):
    """stock_as_of() (reports/services.py) reconstructs a historical
    quantity from the CURRENT one by undoing every dated movement that
    happened after the target date -- there's no day-by-day snapshot to
    just read. Covers each kind of movement it has to undo."""

    def setUp(self):
        self.manager = User.objects.create_user(username='asof-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.today = timezone.localdate()

    def _get(self, **params):
        data = {'as_of_date': self.today.isoformat()}
        data.update(params)
        return self.client.get(reverse('stock_as_of_report'), data)

    def _row(self, response, product):
        return next(r for r in response.context['stock_data'] if r['code'] == product.internal_code)

    def test_requires_login(self):
        self.client.logout()
        response = self._get()
        self.assertEqual(response.status_code, 302)

    def test_late_entered_delivery_counts_by_document_date_not_entry_time(self):
        # Delivery entered into the system NOW (time_of_delivery=now) but
        # dated 3 days ago -- must still count for an "as of 3 days ago"
        # query. This is the exact scenario the user asked for.
        product = Product.objects.create(internal_code='ASF001', name='Late Entry', sell_price=Decimal('5.00'))
        delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, document_type=self.invoice_type, document_number='INV-1',
            document_date=self.today - timezone.timedelta(days=3), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=delivery, delivery_item=product, delivery_quantity=Decimal('10.000'),
        )
        product.refresh_from_db()
        self.assertEqual(product.quantity, Decimal('10.000'))

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=3)).isoformat())
        self.assertEqual(self._row(response, product)['quantity_as_of'], 10.0)

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=4)).isoformat())
        self.assertEqual(self._row(response, product)['quantity_as_of'], 0.0)

    def test_write_off_after_date_is_undone(self):
        product = Product.objects.create(internal_code='ASF002', name='Write-off Undo', sell_price=Decimal('5.00'))
        delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, document_type=self.invoice_type, document_number='INV-2',
            document_date=self.today - timezone.timedelta(days=5), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(delivery=delivery, delivery_item=product, delivery_quantity=Decimal('10.000'))

        writeoff = DeliveryAttributes.objects.create(
            receiver=self.manager, movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            document_date=self.today - timezone.timedelta(days=2), time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(delivery=writeoff, delivery_item=product, delivery_quantity=Decimal('3.000'))

        product.refresh_from_db()
        self.assertEqual(product.quantity, Decimal('7.000'))

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=3)).isoformat())
        self.assertEqual(self._row(response, product)['quantity_as_of'], 10.0)

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=1)).isoformat())
        self.assertEqual(self._row(response, product)['quantity_as_of'], 7.0)

    def test_sale_after_date_is_added_back(self):
        product = Product.objects.create(
            internal_code='ASF003', name='Sale Undo', sell_price=Decimal('5.00'), quantity=Decimal('10'),
        )
        sale = SaleAttributes.objects.create(cashier=self.manager)
        SaleItems.objects.create(
            sale=sale, sale_item=product, sale_quantity=Decimal('4.000'), price_at_sale=Decimal('5.00'),
        )

        product.refresh_from_db()
        self.assertEqual(product.quantity, Decimal('6.000'))

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=1)).isoformat())
        self.assertEqual(self._row(response, product)['quantity_as_of'], 10.0)

        response = self._get()
        self.assertEqual(self._row(response, product)['quantity_as_of'], 6.0)

    def test_revision_correction_is_undone(self):
        product = Product.objects.create(
            internal_code='ASF004', name='Revised', sell_price=Decimal('5.00'), quantity=Decimal('8'),
        )
        revision = RevisionAttributes.objects.create(
            started_by=self.manager, status=RevisionAttributes.STATUS_COMPLETED,
            completed_by=self.manager, completed_at=timezone.now(),
        )
        RevisionItems.objects.create(
            revision=revision, product=product, found_quantity=Decimal('8.000'),
            system_quantity_at_start=Decimal('5.000'),
        )

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=1)).isoformat())
        self.assertEqual(self._row(response, product)['quantity_as_of'], 5.0)

    def test_recipe_excluded_but_ingredient_consumption_reconstructed(self):
        milk = Product.objects.create(
            internal_code='ASF005', name='Milk', sell_price=Decimal('2.00'),
            unit_type=Product.WEIGHT, quantity=Decimal('10'),
        )
        cappuccino = Product.objects.create(
            internal_code='ASF006', name='Cappuccino', sell_price=Decimal('3.00'),
            is_recipe=True, quantity=Decimal('0'),
        )
        RecipeIngredient.objects.create(recipe=cappuccino, ingredient=milk, quantity=Decimal('0.200'))

        sale = SaleAttributes.objects.create(cashier=self.manager)
        SaleItems.objects.create(
            sale=sale, sale_item=cappuccino, sale_quantity=Decimal('5.000'), price_at_sale=Decimal('3.00'),
        )

        milk.refresh_from_db()
        self.assertEqual(milk.quantity, Decimal('9.000'))  # 10 - 0.2 * 5

        response = self._get()
        codes = [r['code'] for r in response.context['stock_data']]
        self.assertNotIn(cappuccino.internal_code, codes)

        response = self._get(as_of_date=(self.today - timezone.timedelta(days=1)).isoformat())
        self.assertEqual(self._row(response, milk)['quantity_as_of'], 10.0)


class ExpiringProductsReportViewTests(TestCase):
    """Batch-level, matches the existing scrap "pick a batch" picker
    (BatchSearchView) -- only DELIVERY items with an expiry date."""

    def setUp(self):
        self.manager = User.objects.create_user(username='exp-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.today = timezone.localdate()
        self.product = Product.objects.create(internal_code='EXP001', name='Yogurt', sell_price=Decimal('2.00'))
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, document_type=self.invoice_type, document_number='INV-3',
            document_date=self.today, time_of_delivery=timezone.now(),
        )

    def _get(self, **params):
        return self.client.get(reverse('expiring_report'), params)

    def test_requires_login(self):
        self.client.logout()
        response = self._get()
        self.assertEqual(response.status_code, 302)

    def test_batch_within_window_included_far_batch_excluded(self):
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product, delivery_quantity=Decimal('5.000'),
            expiry_date=self.today + timezone.timedelta(days=10),
        )
        far_product = Product.objects.create(internal_code='EXP002', name='Canned Beans', sell_price=Decimal('1.50'))
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=far_product, delivery_quantity=Decimal('5.000'),
            expiry_date=self.today + timezone.timedelta(days=365),
        )
        response = self._get(days_ahead=30)
        codes = [r['code'] for r in response.context['batches_data']]
        self.assertIn(self.product.internal_code, codes)
        self.assertNotIn(far_product.internal_code, codes)

    def test_already_expired_batch_always_included(self):
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product, delivery_quantity=Decimal('5.000'),
            expiry_date=self.today - timezone.timedelta(days=5),
        )
        response = self._get(days_ahead=0)
        row = next(r for r in response.context['batches_data'] if r['code'] == self.product.internal_code)
        self.assertEqual(row['days_left'], -5)

    def test_write_off_batches_excluded(self):
        writeoff = DeliveryAttributes.objects.create(
            receiver=self.manager, movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            document_date=self.today, time_of_delivery=timezone.now(),
        )
        DeliveryItems.objects.create(
            delivery=writeoff, delivery_item=self.product, delivery_quantity=Decimal('5.000'),
            expiry_date=self.today + timezone.timedelta(days=5),
        )
        response = self._get(days_ahead=30)
        codes = [r['code'] for r in response.context['batches_data']]
        self.assertNotIn(self.product.internal_code, codes)


class SalesQuantityReportViewTests(TestCase):
    """Shows delivery_price (cost) per product -- Manager/Warehouse only,
    same policy as DeliveriesReportView above."""

    def setUp(self):
        self.manager = User.objects.create_user(username='sq-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.category = Category.objects.create(name='Dairy')
        self.product = Product.objects.create(
            internal_code='SQ001', name='Cheese', category=self.category,
            delivery_price=Decimal('4.00'), sell_price=Decimal('7.00'), quantity=Decimal('10'),
        )
        self.sale = SaleAttributes.objects.create(cashier=self.manager)
        SaleItems.objects.create(
            sale=self.sale, sale_item=self.product, sale_quantity=Decimal('3.000'), price_at_sale=Decimal('7.00'),
        )

    def _get(self, **params):
        today = timezone.localdate()
        data = {
            'start_date': (today - timezone.timedelta(days=7)).isoformat(),
            'end_date': today.isoformat(),
        }
        data.update(params)
        return self.client.get(reverse('sales_quantity_report'), data)

    def test_cashier_forbidden(self):
        cashier = User.objects.create_user(username='sq-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(cashier)
        response = self._get()
        self.assertEqual(response.status_code, 403)

    def test_manager_sees_quantity_category_and_prices(self):
        response = self._get()
        row = next(r for r in response.context['sales_quantity_data'] if r['code'] == self.product.internal_code)
        self.assertEqual(row['quantity_sold'], 3.0)
        self.assertEqual(row['category'], 'Dairy')
        self.assertEqual(row['delivery_price'], 4.0)
        self.assertEqual(row['sell_price'], 7.0)
        self.assertEqual(row['total_amount'], 21.0)

    def test_product_with_no_sales_in_period_not_listed(self):
        other = Product.objects.create(internal_code='SQ002', name='Butter', sell_price=Decimal('3.00'))
        response = self._get()
        codes = [r['code'] for r in response.context['sales_quantity_data']]
        self.assertNotIn(other.internal_code, codes)


class AIReportPermissionTests(TestCase):
    """AI mode can read literally any table (see ai_service.py) -- narrower
    audience than the merely cost-sensitive reports above: Manager only,
    not Manager/Warehouse."""

    def setUp(self):
        self.report = AIReport.objects.create(prompt='How many products do we have?')

    def _login_cashier(self):
        cashier = User.objects.create_user(username='ai-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(cashier)
        return cashier

    def _login_manager(self):
        manager = User.objects.create_user(username='ai-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(manager)
        return manager

    def test_cashier_forbidden_from_list(self):
        self._login_cashier()
        response = self.client.get(reverse('ai_report_list'))
        self.assertEqual(response.status_code, 403)

    def test_cashier_forbidden_from_create(self):
        self._login_cashier()
        response = self.client.get(reverse('ai_report_create'))
        self.assertEqual(response.status_code, 403)

    def test_cashier_forbidden_from_detail(self):
        self._login_cashier()
        response = self.client.get(reverse('ai_report_detail', args=[self.report.pk]))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_view_list(self):
        self._login_manager()
        response = self.client.get(reverse('ai_report_list'))
        self.assertEqual(response.status_code, 200)

    def test_requires_login(self):
        response = self.client.get(reverse('ai_report_list'))
        self.assertEqual(response.status_code, 302)


class AIReportCreateViewTests(TestCase):
    """Mocks run_ai_report throughout -- it calls the real Anthropic API
    and the real 'readonly' Postgres role, neither of which is available
    (or wanted) in the automated test suite, same reasoning as
    dispatch_task/Celery being unreachable in this environment (see
    CLAUDE.md). Confirmed empirically that the 'readonly' role's grants
    don't carry over to the Django test database (permission denied for
    any real table), so there's no way to exercise the real SQL path here
    -- only the view wiring around it."""

    def setUp(self):
        self.manager = User.objects.create_user(username='ai-create-mgr', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)

    def test_empty_prompt_shows_error_without_creating_report(self):
        response = self.client.post(reverse('ai_report_create'), {'prompt': '   '})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Type a question first')
        self.assertEqual(AIReport.objects.count(), 0)

    @patch('STORA.reports.views.run_ai_report')
    def test_successful_prompt_creates_report_and_redirects(self, mock_run):
        mock_run.return_value = {
            'answer': 'You have 3 products.', 'sql': 'SELECT count(*) FROM products_product',
            'columns': ['count'], 'rows': [[3]],
        }
        response = self.client.post(reverse('ai_report_create'), {'prompt': 'How many products?'})
        self.assertEqual(AIReport.objects.count(), 1)
        report = AIReport.objects.get()
        self.assertEqual(report.prompt, 'How many products?')
        self.assertEqual(report.last_answer, 'You have 3 products.')
        self.assertEqual(report.created_by, self.manager)
        self.assertIsNotNone(report.last_regenerated_at)
        self.assertRedirects(response, reverse('ai_report_detail', args=[report.pk]))

    @patch('STORA.reports.views.run_ai_report')
    def test_not_configured_shows_friendly_error_not_500(self, mock_run):
        mock_run.side_effect = AIReportsNotConfigured
        response = self.client.post(reverse('ai_report_create'), {'prompt': 'How many products?'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ANTHROPIC_API_KEY')
        self.assertEqual(AIReport.objects.count(), 0)

    @patch('STORA.reports.views.run_ai_report')
    def test_api_error_shows_message_without_creating_report(self, mock_run):
        mock_run.side_effect = RuntimeError('boom')
        response = self.client.post(reverse('ai_report_create'), {'prompt': 'How many products?'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'boom')
        self.assertEqual(AIReport.objects.count(), 0)


class AIReportDetailViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='ai-detail-mgr', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.report = AIReport.objects.create(
            prompt='How many products?', generated_sql='SELECT count(*) FROM products_product',
            last_answer='You have 3 products.',
        )

    @patch('STORA.reports.views.rerun_stored_query')
    def test_detail_shows_freshly_rerun_data(self, mock_rerun):
        mock_rerun.return_value = (['count'], [[5]], None)
        response = self.client.get(reverse('ai_report_detail', args=[self.report.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['columns'], ['count'])
        self.assertEqual(response.context['table_data'], [{'count': 5}])
        self.assertIsNone(response.context['rerun_error'])
        mock_rerun.assert_called_once_with(self.report.generated_sql)

    @patch('STORA.reports.views.rerun_stored_query')
    def test_detail_shows_rerun_error_when_stored_sql_now_fails(self, mock_rerun):
        mock_rerun.return_value = ([], [], 'relation "x" does not exist')
        response = self.client.get(reverse('ai_report_detail', args=[self.report.pk]))
        self.assertContains(response, 'no longer runs')


class AIReportRegenerateViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='ai-regen-mgr', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.report = AIReport.objects.create(prompt='How many products?', generated_sql='SELECT 1', last_answer='old')

    @patch('STORA.reports.views.rerun_stored_query')
    @patch('STORA.reports.views.run_ai_report')
    def test_regenerate_updates_sql_and_answer(self, mock_run, mock_rerun):
        mock_run.return_value = {
            'answer': 'new answer', 'sql': 'SELECT 2', 'columns': [], 'rows': [],
        }
        mock_rerun.return_value = ([], [], None)
        response = self.client.post(reverse('ai_report_regenerate', args=[self.report.pk]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.generated_sql, 'SELECT 2')
        self.assertEqual(self.report.last_answer, 'new answer')
        self.assertIsNotNone(self.report.last_regenerated_at)
        self.assertRedirects(response, reverse('ai_report_detail', args=[self.report.pk]))

    @patch('STORA.reports.views.run_ai_report')
    def test_regenerate_failure_leaves_report_unchanged(self, mock_run):
        mock_run.side_effect = RuntimeError('boom')
        self.client.post(reverse('ai_report_regenerate', args=[self.report.pk]))
        self.report.refresh_from_db()
        self.assertEqual(self.report.generated_sql, 'SELECT 1')
        self.assertEqual(self.report.last_answer, 'old')

    def test_cashier_forbidden(self):
        cashier = User.objects.create_user(username='ai-regen-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(cashier)
        response = self.client.post(reverse('ai_report_regenerate', args=[self.report.pk]))
        self.assertEqual(response.status_code, 403)


class AIReportDeleteViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='ai-delete-mgr', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.report = AIReport.objects.create(prompt='How many products?')

    def test_manager_can_delete(self):
        response = self.client.post(reverse('ai_report_delete', args=[self.report.pk]))
        self.assertRedirects(response, reverse('ai_report_list'))
        self.assertEqual(AIReport.objects.count(), 0)

    def test_cashier_forbidden(self):
        cashier = User.objects.create_user(username='ai-delete-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(cashier)
        response = self.client.post(reverse('ai_report_delete', args=[self.report.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AIReport.objects.count(), 1)
