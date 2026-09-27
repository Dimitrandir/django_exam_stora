from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from STORA.accounts.models import CompanyProfile
from STORA.orders.ai_service import _extract_quantities
from STORA.orders.models import OrderAttributes, OrderItems
from STORA.products.models import Product, ProductSupplier, Suppliers
from STORA.reports.ai_service import AIReportsNotConfigured
from STORA.sales.models import SaleAttributes, SaleItems

User = get_user_model()


class OrderAttributesModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr', password='pass12345', role=User.MANAGER)
        self.supplier = Suppliers.objects.create(name='Test Supplier', bulstat='111111111')

    def test_order_number_is_numeric_starting_from_one(self):
        order = OrderAttributes.objects.create(
            supplier=self.supplier, period_start=timezone.localdate() - timedelta(days=7),
            period_end=timezone.localdate(), created_by=self.user,
        )
        self.assertTrue(order.order_number.isdigit())
        self.assertEqual(order.order_number, '1')

    def test_second_order_same_day_gets_distinct_number(self):
        today = timezone.localdate()
        order1 = OrderAttributes.objects.create(
            supplier=self.supplier, order_date=today, period_start=today, period_end=today, created_by=self.user,
        )
        order2 = OrderAttributes.objects.create(
            supplier=self.supplier, order_date=today, period_start=today, period_end=today, created_by=self.user,
        )
        self.assertNotEqual(order1.order_number, order2.order_number)


class CompanyProfileTests(TestCase):
    def test_get_solo_creates_and_reuses_one_row(self):
        first = CompanyProfile.get_solo()
        second = CompanyProfile.get_solo()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CompanyProfile.objects.count(), 1)

    def test_company_profile_edit_requires_manager_permission(self):
        User.objects.create_user(username='cashier', password='pass12345', role=User.CASHIER)
        self.client.login(username='cashier', password='pass12345')
        response = self.client.get(reverse('company_profile_edit'))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_save_company_profile(self):
        User.objects.create_user(username='boss', password='pass12345', role=User.MANAGER)
        self.client.login(username='boss', password='pass12345')
        response = self.client.post(reverse('company_profile_edit'), {
            'name': 'STORA Shop', 'bulstat': '123456789', 'vat_n': '', 'address': 'Main St 1',
        })
        self.assertRedirects(response, reverse('employee_list'))
        profile = CompanyProfile.get_solo()
        self.assertEqual(profile.name, 'STORA Shop')


class OrderViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wh', password='pass12345', role=User.WAREHOUSE)
        self.client.login(username='wh', password='pass12345')

        self.supplier = Suppliers.objects.create(name='Fresh Foods', bulstat='222222222')
        self.other_supplier = Suppliers.objects.create(name='Other Co', bulstat='333333333')

        self.product = Product.objects.create(
            internal_code='ORD0001', name='Orderable Product', delivery_price=1, sell_price=2, quantity=10,
        )
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier, position=1)

        # Not linked to this supplier at position 1 -- should never show up
        # as a candidate for it.
        self.unrelated_product = Product.objects.create(
            internal_code='ORD0002', name='Unrelated Product', delivery_price=1, sell_price=2, quantity=5,
        )

    def _make_sale(self, product, qty):
        sale = SaleAttributes.objects.create(cashier=self.user, payment_method=SaleAttributes.CASH, amount_paid=100)
        SaleItems.objects.create(sale=sale, sale_item=product, sale_quantity=qty, price_at_sale=product.sell_price)
        return sale

    def test_order_new_page_loads(self):
        response = self.client.get(reverse('order_new'))
        self.assertEqual(response.status_code, 200)

    def test_loading_candidates_only_shows_products_at_that_position(self):
        response = self.client.get(reverse('order_new'), {
            'supplier': self.supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        self.assertEqual(response.status_code, 200)
        lines = response.context['lines_data']
        product_ids = [line['product_id'] for line in lines]
        self.assertIn(self.product.pk, product_ids)
        self.assertNotIn(self.unrelated_product.pk, product_ids)

    def test_candidate_sold_qty_reflects_period_sales(self):
        self._make_sale(self.product, Decimal('3'))
        response = self.client.get(reverse('order_new'), {
            'supplier': self.supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        line = next(l for l in response.context['lines_data'] if l['product_id'] == self.product.pk)
        self.assertEqual(line['sold_qty'], 3.0)
        self.assertEqual(line['sale_count'], 1)

    def test_posting_an_order_creates_attributes_and_items(self):
        response = self.client.post(reverse('order_new'), {
            'supplier': self.supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
            f'qty_{self.product.pk}': '6',
        })
        order = OrderAttributes.objects.get()
        self.assertRedirects(response, reverse('order_details', kwargs={'pk': order.pk}))
        self.assertEqual(order.supplier, self.supplier)
        item = OrderItems.objects.get(order=order)
        self.assertEqual(item.product, self.product)
        self.assertEqual(item.requested_quantity, Decimal('6'))

    def test_posting_with_no_quantities_shows_error_without_saving(self):
        response = self.client.post(reverse('order_new'), {
            'supplier': self.supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter at least one requested quantity')
        self.assertFalse(OrderAttributes.objects.exists())

    def test_order_details_page_loads(self):
        order = OrderAttributes.objects.create(
            supplier=self.supplier, period_start=timezone.localdate() - timedelta(days=7),
            period_end=timezone.localdate(), created_by=self.user,
        )
        OrderItems.objects.create(order=order, product=self.product, requested_quantity=Decimal('4'))
        response = self.client.get(reverse('order_details', kwargs={'pk': order.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_number)

    def test_order_list_page_loads(self):
        response = self.client.get(reverse('order_list'))
        self.assertEqual(response.status_code, 200)

    def test_cashier_cannot_create_orders(self):
        User.objects.create_user(username='cashier2', password='pass12345', role=User.CASHIER)
        self.client.login(username='cashier2', password='pass12345')
        response = self.client.get(reverse('order_new'))
        self.assertEqual(response.status_code, 403)


class OrderMultiPositionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wh3', password='pass12345', role=User.WAREHOUSE)
        self.client.login(username='wh3', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Multi Supplier', bulstat='666666666')
        self.primary_product = Product.objects.create(
            internal_code='MP0001', name='Primary Product', delivery_price=1, sell_price=2, quantity=10,
        )
        self.secondary_product = Product.objects.create(
            internal_code='MP0002', name='Secondary Product', delivery_price=1, sell_price=2, quantity=10,
        )
        ProductSupplier.objects.create(product=self.primary_product, supplier=self.supplier, position=1)
        ProductSupplier.objects.create(product=self.secondary_product, supplier=self.supplier, position=2)

    def test_ticking_two_positions_pulls_candidates_from_both(self):
        response = self.client.get(reverse('order_new'), {
            'supplier': self.supplier.pk, 'supplier_position': [1, 2],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        product_ids = [line['product_id'] for line in response.context['lines_data']]
        self.assertIn(self.primary_product.pk, product_ids)
        self.assertIn(self.secondary_product.pk, product_ids)

    def test_position_field_stores_both_selected_tiers(self):
        self.client.post(reverse('order_new'), {
            'supplier': self.supplier.pk, 'supplier_position': [1, 2],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
            f'qty_{self.primary_product.pk}': '3', f'qty_{self.secondary_product.pk}': '4',
        })
        order = OrderAttributes.objects.get()
        self.assertEqual(order.get_position_list(), [1, 2])
        self.assertEqual(order.get_supplier_position_display(), 'Primary (1st), Secondary (2nd)')


class OrderEditTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wh4', password='pass12345', role=User.WAREHOUSE)
        self.client.login(username='wh4', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Edit Supplier', bulstat='777777777')
        self.product = Product.objects.create(
            internal_code='ED0001', name='Editable Product', delivery_price=1, sell_price=2, quantity=10,
        )
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier, position=1)
        self.order = OrderAttributes.objects.create(
            supplier=self.supplier, period_start=timezone.localdate() - timedelta(days=7),
            period_end=timezone.localdate(), created_by=self.user,
        )
        self.order.set_position_list([1])
        self.order.save(update_fields=['supplier_position'])
        OrderItems.objects.create(order=self.order, product=self.product, requested_quantity=Decimal('5'))

    def test_order_edit_page_loads_with_existing_quantity(self):
        response = self.client.get(reverse('order_edit', kwargs={'pk': self.order.pk}))
        self.assertEqual(response.status_code, 200)
        line = next(l for l in response.context['lines_data'] if l['product_id'] == self.product.pk)
        self.assertEqual(line['requested_qty'], 5.0)

    def test_order_edit_replaces_items(self):
        response = self.client.post(reverse('order_edit', kwargs={'pk': self.order.pk}), {
            f'qty_{self.product.pk}': '9',
        })
        self.assertRedirects(response, reverse('order_details', kwargs={'pk': self.order.pk}))
        item = OrderItems.objects.get(order=self.order)
        self.assertEqual(item.requested_quantity, Decimal('9'))

    def test_warehouse_cannot_edit_without_permission_change_denied_for_cashier(self):
        User.objects.create_user(username='cashier3', password='pass12345', role=User.CASHIER)
        self.client.login(username='cashier3', password='pass12345')
        response = self.client.get(reverse('order_edit', kwargs={'pk': self.order.pk}))
        self.assertEqual(response.status_code, 403)


class OrderTotalAndPickerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wh5', password='pass12345', role=User.WAREHOUSE)
        self.client.login(username='wh5', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Total Supplier', bulstat='888888888')
        self.product = Product.objects.create(
            internal_code='TOT0001', name='Total Product', delivery_price=Decimal('2.50'), sell_price=4, quantity=10,
        )
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier, position=1)
        self.order = OrderAttributes.objects.create(
            supplier=self.supplier, period_start=timezone.localdate() - timedelta(days=7),
            period_end=timezone.localdate(), created_by=self.user,
        )
        OrderItems.objects.create(order=self.order, product=self.product, requested_quantity=Decimal('4'))

    def test_order_list_shows_computed_total(self):
        response = self.client.get(reverse('order_list'))
        row = next(o for o in response.context['orders_data'] if o['id'] == self.order.pk)
        self.assertEqual(row['total_amount'], 10.0)  # 4 * 2.50

    def test_order_items_json_returns_supplier_and_items(self):
        response = self.client.get(reverse('order_items_json', kwargs={'pk': self.order.pk}))
        data = response.json()
        self.assertEqual(data['supplier_id'], self.supplier.pk)
        self.assertEqual(data['items'], [{'product_id': self.product.pk, 'quantity': 4.0}])

    def test_order_picker_json_lists_recent_orders(self):
        response = self.client.get(reverse('order_picker_json'))
        data = response.json()
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['order_number'], self.order.order_number)


class OrderAiSuggestViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='wh2', password='pass12345', role=User.WAREHOUSE)
        self.client.login(username='wh2', password='pass12345')
        self.supplier = Suppliers.objects.create(name='AI Supplier', bulstat='444444444')
        self.product = Product.objects.create(
            internal_code='AIP0001', name='AI Product', delivery_price=1, sell_price=2, quantity=10,
        )
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier, position=1)

    def test_ai_suggest_without_api_key_returns_friendly_error(self):
        response = self.client.post(reverse('order_ai_suggest'), {
            'supplier_id': self.supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('ANTHROPIC_API_KEY', response.json()['error'])

    def test_ai_suggest_with_no_candidates_returns_empty_list(self):
        other_supplier = Suppliers.objects.create(name='No Products Co', bulstat='555555555')
        response = self.client.post(reverse('order_ai_suggest'), {
            'supplier_id': other_supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['suggestions'], [])

    def test_ai_suggest_missing_fields_returns_400(self):
        response = self.client.post(reverse('order_ai_suggest'), {})
        self.assertEqual(response.status_code, 400)

    @patch('STORA.orders.views.suggest_order_quantities')
    def test_ai_suggest_returns_suggestions_from_service(self, mock_suggest):
        mock_suggest.return_value = [{'product_id': self.product.pk, 'quantity': 5}]
        response = self.client.post(reverse('order_ai_suggest'), {
            'supplier_id': self.supplier.pk, 'supplier_position': [1],
            'start_date': timezone.localdate() - timedelta(days=7), 'end_date': timezone.localdate(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['suggestions'], [{'product_id': self.product.pk, 'quantity': 5}])


class OrderAiDraftAllViewTests(TestCase):
    def setUp(self):
        self.wh_user = User.objects.create_user(username='wh3', password='pass12345', role=User.WAREHOUSE)
        self.mgr_user = User.objects.create_user(username='mgr3', password='pass12345', role=User.MANAGER)
        self.cashier = User.objects.create_user(username='cash3', password='pass12345', role=User.CASHIER)

        self.supplier_a = Suppliers.objects.create(name='Draftable Supplier', bulstat='666666666')
        self.product_a = Product.objects.create(
            internal_code='DFT0001', name='Draftable Product', delivery_price=2, sell_price=4, quantity=3,
        )
        ProductSupplier.objects.create(product=self.product_a, supplier=self.supplier_a, position=1)

        self.supplier_b = Suppliers.objects.create(name='Nothing To Order Co', bulstat='777777777')
        self.product_b = Product.objects.create(
            internal_code='DFT0002', name='Slow Mover', delivery_price=1, sell_price=2, quantity=50,
        )
        ProductSupplier.objects.create(product=self.product_b, supplier=self.supplier_b, position=1)

        # No ProductSupplier link at all -- must be skipped without even
        # being counted as "AI found nothing", let alone calling the API.
        Suppliers.objects.create(name='Empty Supplier Co', bulstat='888888888')

    def _draft_side_effect(self, supplier_name, positions, start_date, end_date, candidates):
        if supplier_name == self.supplier_a.name:
            return [{'product_id': self.product_a.pk, 'quantity': 6}]
        return []

    def test_without_api_key_redirects_without_creating_anything(self):
        self.client.login(username='wh3', password='pass12345')
        response = self.client.post(reverse('order_ai_draft_all'))
        self.assertRedirects(response, reverse('order_list'))
        self.assertEqual(OrderAttributes.objects.count(), 0)

    def test_cashier_cannot_trigger_ai_draft_all(self):
        self.client.login(username='cash3', password='pass12345')
        response = self.client.post(reverse('order_ai_draft_all'))
        self.assertEqual(response.status_code, 403)

    @override_settings(ANTHROPIC_API_KEY='fake-key-for-tests')
    @patch('STORA.orders.views.suggest_order_quantities')
    def test_creates_draft_orders_only_where_ai_suggests_something(self, mock_suggest):
        mock_suggest.side_effect = self._draft_side_effect
        self.client.login(username='wh3', password='pass12345')

        response = self.client.post(reverse('order_ai_draft_all'))

        self.assertRedirects(
            response, f"{reverse('order_list')}?ai_drafted=1&ai_skipped=1", fetch_redirect_response=False,
        )
        self.assertEqual(OrderAttributes.objects.count(), 1)
        order = OrderAttributes.objects.get()
        self.assertEqual(order.supplier, self.supplier_a)
        self.assertEqual(order.status, OrderAttributes.STATUS_DRAFT)
        item = order.items.get()
        self.assertEqual(item.product, self.product_a)
        self.assertEqual(item.requested_quantity, Decimal('6'))


class OrderConfirmDiscardViewTests(TestCase):
    def setUp(self):
        self.wh_user = User.objects.create_user(username='wh4', password='pass12345', role=User.WAREHOUSE)
        self.mgr_user = User.objects.create_user(username='mgr4', password='pass12345', role=User.MANAGER)
        self.supplier = Suppliers.objects.create(name='Draft Review Co', bulstat='999999999')
        self.product = Product.objects.create(
            internal_code='DRV0001', name='Draft Review Product', delivery_price=1, sell_price=2, quantity=5,
        )
        self.draft_order = OrderAttributes.objects.create(
            supplier=self.supplier, period_start=timezone.localdate() - timedelta(days=7),
            period_end=timezone.localdate(), created_by=self.wh_user, status=OrderAttributes.STATUS_DRAFT,
        )
        OrderItems.objects.create(order=self.draft_order, product=self.product, requested_quantity=Decimal('4'))

    def test_confirm_marks_draft_order_as_confirmed(self):
        self.client.login(username='wh4', password='pass12345')
        response = self.client.post(reverse('order_confirm', args=[self.draft_order.pk]))
        self.assertRedirects(response, reverse('order_details', kwargs={'pk': self.draft_order.pk}))
        self.draft_order.refresh_from_db()
        self.assertEqual(self.draft_order.status, OrderAttributes.STATUS_CONFIRMED)

    def test_discard_deletes_draft_order_and_its_items(self):
        self.client.login(username='mgr4', password='pass12345')
        response = self.client.post(reverse('order_discard', args=[self.draft_order.pk]))
        self.assertRedirects(response, reverse('order_list'))
        self.assertEqual(OrderAttributes.objects.filter(pk=self.draft_order.pk).count(), 0)
        self.assertEqual(OrderItems.objects.count(), 0)

    def test_warehouse_cannot_discard_only_manager_can(self):
        self.client.login(username='wh4', password='pass12345')
        response = self.client.post(reverse('order_discard', args=[self.draft_order.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrderAttributes.objects.filter(pk=self.draft_order.pk).count(), 1)

    def test_discard_refuses_a_confirmed_order(self):
        confirmed_order = OrderAttributes.objects.create(
            supplier=self.supplier, period_start=timezone.localdate() - timedelta(days=7),
            period_end=timezone.localdate(), created_by=self.wh_user, status=OrderAttributes.STATUS_CONFIRMED,
        )
        self.client.login(username='mgr4', password='pass12345')
        response = self.client.post(reverse('order_discard', args=[confirmed_order.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(OrderAttributes.objects.filter(pk=confirmed_order.pk).count(), 1)


class ExtractQuantitiesTests(TestCase):
    def test_parses_fenced_json_block(self):
        text = 'Some reasoning here.\n```json\n[{"product_id": 1, "quantity": 5}, {"product_id": 2, "quantity": 0}]\n```'
        result = _extract_quantities(text)
        self.assertEqual(result, [{'product_id': 1, 'quantity': 5.0}, {'product_id': 2, 'quantity': 0.0}])

    def test_returns_empty_list_when_no_json_block(self):
        self.assertEqual(_extract_quantities('No JSON here.'), [])

    def test_skips_malformed_entries(self):
        text = '```json\n[{"product_id": "x", "quantity": 5}, {"product_id": 1, "quantity": 2}]\n```'
        result = _extract_quantities(text)
        self.assertEqual(result, [{'product_id': 1, 'quantity': 2.0}])
