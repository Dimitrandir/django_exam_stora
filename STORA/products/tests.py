from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from STORA.accounts.models import Employee
from STORA.deliveries.models import DeliveryAttributes, DeliveryItems
from STORA.products.forms import ProductForms, RecipeIngredientForm
from STORA.products.models import Product, Barcode, Category, Suppliers, ProductSupplier, RecipeIngredient
from STORA.products.templatetags.currency_filters import quantity_display
from STORA.products.views import get_free_internal_codes
from STORA.sales.models import SaleAttributes, SaleItems


User = get_user_model()


class ProductApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            password='pass12345',
        )
        self.client.login(username='testuser', password='pass12345')

        self.product = Product.objects.create(
            internal_code='P0000001',
            name='Test Product',
            delivery_price=2.50,
            sell_price=3.50,
            quantity=10,
        )
        Barcode.objects.create(
            code='1234567890123',
            product=self.product,
        )

    def test_products_api_returns_200_for_authenticated_user(self):
        response = self.client.get(reverse('api_products'))
        self.assertEqual(response.status_code, 200)

    def test_products_api_returns_product_data(self):
        response = self.client.get(reverse('api_products'))
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]['name'], 'Test Product')

    def test_products_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get(reverse('api_products'))
        self.assertIn(response.status_code, (302, 401, 403))


class ProductPermissionTests(TestCase):
    """Cashiers get read-only access to the product catalog (to look up
    price/stock while ringing up a sale); Warehouse can add/edit but not
    delete; Manager can do everything. Mirrors the groups set up in
    accounts/signals.py."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager1', password='pass12345', role=Employee.MANAGER
        )
        self.cashier = Employee.objects.create_user(
            username='cashier1', password='pass12345', role=Employee.CASHIER
        )
        self.warehouse = Employee.objects.create_user(
            username='warehouse1', password='pass12345', role=Employee.WAREHOUSE
        )
        self.product = Product.objects.create(
            internal_code='P1000001', name='Widget', sell_price=1.99, quantity=5
        )

    def test_cashier_can_view_product_list(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('product_list'))
        self.assertEqual(response.status_code, 200)

    def test_cashier_cannot_create_product(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('product_create'))
        self.assertEqual(response.status_code, 403)

    def test_warehouse_can_create_but_not_delete_product(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('product_create')).status_code, 200)
        response = self.client.get(reverse('product_delete', kwargs={'pk': self.product.pk}))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_delete_product(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_delete', kwargs={'pk': self.product.pk}))
        self.assertEqual(response.status_code, 200)


class ProductModelValidationTests(TestCase):
    def test_sell_price_is_required(self):
        form = ProductForms(data={
            'internal_code': 'P1000002',
            'name': 'No Price Widget',
            'quantity': 0,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('sell_price', form.errors)

    def test_category_is_not_required(self):
        """Regression test: Product.category allows NULL at the model level
        (null=True), but Django derives a ModelForm field's `required` from
        `blank`, not `null` -- without ProductForms.__init__ explicitly
        setting required=False, the rendered <select> got the HTML
        `required` attribute and the browser silently blocked the Save
        click (no request sent at all) whenever category was left unset,
        with no visible error to explain why."""
        form = ProductForms(data={
            'internal_code': 'P1000012', 'name': 'No Category Widget', 'unit_type': 'piece',
            'sell_price': '1.00', 'quantity': 0,
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_duplicate_barcode_is_rejected(self):
        product_a = Product.objects.create(internal_code='P1000003', name='A', sell_price=1, quantity=0)
        product_b = Product.objects.create(internal_code='P1000004', name='B', sell_price=1, quantity=0)
        Barcode.objects.create(code='1111111111111', product=product_a)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Barcode.objects.create(code='1111111111111', product=product_b)


class ProductQuantityFieldTests(TestCase):
    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager2', password='pass12345', role=Employee.MANAGER
        )
        self.product = Product.objects.create(
            internal_code='P1000005', name='Gadget', sell_price=2.5, quantity=10
        )

    def test_quantity_cannot_be_changed_through_edit_form(self):
        self.client.force_login(self.manager)
        self.client.post(reverse('product_edit', kwargs={'pk': self.product.pk}), {
            'internal_code': self.product.internal_code,
            'name': self.product.name,
            'sell_price': '2.50',
            'quantity': '9999',
            'barcode-TOTAL_FORMS': '0',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '0',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
        })
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 10)


class ProductDeleteProtectedTests(TestCase):
    def test_deleting_product_with_sale_history_shows_friendly_error(self):
        manager = Employee.objects.create_user(
            username='manager3', password='pass12345', role=Employee.MANAGER
        )
        product = Product.objects.create(
            internal_code='P1000006', name='Sold Thing', sell_price=5, quantity=10
        )
        sale = SaleAttributes.objects.create(cashier=manager)
        SaleItems.objects.create(sale=sale, sale_item=product, sale_quantity=1)

        self.client.force_login(manager)
        response = self.client.post(reverse('product_delete', kwargs={'pk': product.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertContains(response, 'Cannot delete')


class ProductBarcodeFormsetTests(TestCase):
    def test_product_create_with_multiple_barcodes_succeeds(self):
        manager = Employee.objects.create_user(
            username='manager5', password='pass12345', role=Employee.MANAGER
        )
        category = Category.objects.create(name='Test Category')
        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'P1000009',
            'name': 'Multi Barcode Widget',
            'unit_type': 'piece',
            'sell_price': '3.00',
            'quantity': '0',
            'category': category.pk,
            'barcode-TOTAL_FORMS': '2',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'barcode-0-code': '1111111111116',
            'barcode-1-code': '2222222222225',
            'supplier-TOTAL_FORMS': '0',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
            'recipe-TOTAL_FORMS': '0',
            'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0',
            'recipe-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(internal_code='P1000009')
        self.assertEqual(
            set(product.barcode.values_list('code', flat=True)),
            {'1111111111116', '2222222222225'},
        )

    def test_invalid_barcode_formset_does_not_silently_succeed(self):
        manager = Employee.objects.create_user(
            username='manager4', password='pass12345', role=Employee.MANAGER
        )
        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'P1000007',
            'name': 'New Widget',
            'sell_price': '3.00',
            'quantity': '0',
            'barcode-TOTAL_FORMS': '1',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'barcode-0-code': 'way-too-long-to-be-a-valid-barcode-value',
            'supplier-TOTAL_FORMS': '0',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(internal_code='P1000007').exists())


class ProductHistoryViewTests(TestCase):
    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager6', password='pass12345', role=Employee.MANAGER
        )
        self.product = Product.objects.create(
            internal_code='P1000010', name='History Widget', sell_price=2.0, quantity=0
        )
        self.supplier = Suppliers.objects.create(name='Acme Ltd', bulstat='123456789')
        self.today = timezone.localdate()

        sale = SaleAttributes.objects.create(cashier=self.manager)
        SaleItems.objects.create(sale=sale, sale_item=self.product, sale_quantity=3)

        delivery = DeliveryAttributes.objects.create(
            receiver=self.manager,
            supplier=self.supplier,
            document_type='INVOICE',
            document_date=self.today,
        )
        DeliveryItems.objects.create(
            delivery=delivery, delivery_item=self.product, delivery_quantity=10, price_at_delivery=1.5
        )

    def test_cashier_can_view_history(self):
        cashier = Employee.objects.create_user(
            username='cashierhist', password='pass12345', role=Employee.CASHIER
        )
        self.client.force_login(cashier)
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        self.assertEqual(response.status_code, 200)

    def test_history_shows_sale_and_delivery_events(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        self.assertEqual(response.status_code, 200)

        events = response.context['events']
        self.assertEqual(len(events), 2)

        sale_event = next(e for e in events if e['event_type'] == 'Sale')
        self.assertEqual(sale_event['quantity_change'], -3)
        self.assertIsNone(sale_event['supplier'])

        delivery_event = next(e for e in events if e['event_type'] == 'Delivery')
        self.assertEqual(delivery_event['quantity_change'], 10)
        self.assertEqual(delivery_event['supplier'], self.supplier)

    def test_history_filters_out_events_outside_selected_period(self):
        self.client.force_login(self.manager)
        old_start = self.today - timedelta(days=400)
        old_end = self.today - timedelta(days=390)
        response = self.client.get(
            reverse('product_history', kwargs={'pk': self.product.pk}),
            {'start_date': old_start.isoformat(), 'end_date': old_end.isoformat()},
        )
        self.assertEqual(len(response.context['events']), 0)


class ProductInlineUpdateViewTests(TestCase):
    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager7', password='pass12345', role=Employee.MANAGER
        )
        self.cashier = Employee.objects.create_user(
            username='cashier2', password='pass12345', role=Employee.CASHIER
        )
        self.category = Category.objects.create(name='Drinks')
        self.supplier = Suppliers.objects.create(name='Acme Inline Ltd', bulstat='987654321')
        self.product = Product.objects.create(
            internal_code='P1000011', name='Inline Widget', sell_price=1.50,
            quantity=7, category=self.category,
        )
        self.product.supplier.add(self.supplier)

    def _post(self, field, value):
        return self.client.post(
            reverse('product_inline_update', kwargs={'pk': self.product.pk}),
            {'field': field, 'value': value},
        )

    def test_cashier_cannot_inline_edit(self):
        self.client.force_login(self.cashier)
        response = self._post('name', 'Hacked Name')
        self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Inline Widget')

    def test_manager_can_edit_name(self):
        self.client.force_login(self.manager)
        response = self._post('name', 'Renamed Widget')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Renamed Widget')

    def test_manager_can_edit_sell_price(self):
        self.client.force_login(self.manager)
        response = self._post('sell_price', '4.25')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(str(self.product.sell_price), '4.25')

    def test_invalid_sell_price_is_rejected(self):
        self.client.force_login(self.manager)
        response = self._post('sell_price', '-5')
        self.assertEqual(response.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(str(self.product.sell_price), '1.50')

    def test_category_change_resolves_by_name(self):
        self.client.force_login(self.manager)
        other_category = Category.objects.create(name='Snacks')
        response = self._post('category', 'Snacks')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.category, other_category)

    def test_category_can_be_cleared(self):
        self.client.force_login(self.manager)
        response = self._post('category', '')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertIsNone(self.product.category)

    def test_unknown_category_name_rejected(self):
        self.client.force_login(self.manager)
        response = self._post('category', 'Does Not Exist')
        self.assertEqual(response.status_code, 400)

    def test_quantity_field_cannot_be_edited_this_way(self):
        self.client.force_login(self.manager)
        response = self._post('quantity', '9999')
        self.assertEqual(response.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 7)

    def test_inline_edit_does_not_touch_unrelated_fields(self):
        """Regression guard: since ProductInlineEditForm only lists 4
        fields, saving it must never wipe unrelated ones like `quantity`
        or the `supplier` M2M -- the classic ModelForm gotcha where an
        omitted M2M field gets cleared on save()."""
        self.client.force_login(self.manager)
        response = self._post('name', 'Still Has Supplier')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 7)
        self.assertEqual(list(self.product.supplier.all()), [self.supplier])


class ProductBulkActionViewTests(TestCase):
    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager8', password='pass12345', role=Employee.MANAGER
        )
        self.warehouse = Employee.objects.create_user(
            username='warehouse2', password='pass12345', role=Employee.WAREHOUSE
        )
        self.cashier = Employee.objects.create_user(
            username='cashier3', password='pass12345', role=Employee.CASHIER
        )
        self.category_a = Category.objects.create(name='Bulk A')
        self.category_b = Category.objects.create(name='Bulk B')
        self.product1 = Product.objects.create(
            internal_code='P2000001', name='Bulk One', sell_price=1, quantity=0, category=self.category_a
        )
        self.product2 = Product.objects.create(
            internal_code='P2000002', name='Bulk Two', sell_price=2, quantity=0, category=self.category_a
        )

    def _post(self, action, ids, value=None):
        data = {'action': action, 'ids': ids}
        if value is not None:
            data['value'] = value
        return self.client.post(reverse('product_bulk_action'), data)

    def test_cashier_cannot_bulk_change_category(self):
        self.client.force_login(self.cashier)
        response = self._post('set_category', [self.product1.pk, self.product2.pk], 'Bulk B')
        self.assertEqual(response.status_code, 403)
        self.product1.refresh_from_db()
        self.assertEqual(self.product1.category, self.category_a)

    def test_manager_can_bulk_change_category(self):
        self.client.force_login(self.manager)
        response = self._post('set_category', [self.product1.pk, self.product2.pk], 'Bulk B')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['updated'], 2)
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertEqual(self.product1.category, self.category_b)
        self.assertEqual(self.product2.category, self.category_b)

    def test_bulk_category_can_clear(self):
        self.client.force_login(self.manager)
        response = self._post('set_category', [self.product1.pk], '')
        self.assertEqual(response.status_code, 200)
        self.product1.refresh_from_db()
        self.assertIsNone(self.product1.category)

    def test_bulk_set_sell_price(self):
        self.client.force_login(self.warehouse)
        response = self._post('set_sell_price', [self.product1.pk, self.product2.pk], '9.99')
        self.assertEqual(response.status_code, 200)
        self.product1.refresh_from_db()
        self.product2.refresh_from_db()
        self.assertEqual(str(self.product1.sell_price), '9.99')
        self.assertEqual(str(self.product2.sell_price), '9.99')

    def test_bulk_set_sell_price_rejects_invalid(self):
        self.client.force_login(self.manager)
        response = self._post('set_sell_price', [self.product1.pk], '0')
        self.assertEqual(response.status_code, 400)
        self.product1.refresh_from_db()
        self.assertEqual(str(self.product1.sell_price), '1.00')

    def test_warehouse_cannot_bulk_delete(self):
        self.client.force_login(self.warehouse)
        response = self._post('delete', [self.product1.pk])
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Product.objects.filter(pk=self.product1.pk).exists())

    def test_manager_can_bulk_delete(self):
        self.client.force_login(self.manager)
        response = self._post('delete', [self.product1.pk, self.product2.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deleted'], 2)
        self.assertFalse(Product.objects.filter(pk__in=[self.product1.pk, self.product2.pk]).exists())

    def test_bulk_delete_skips_protected_but_deletes_rest(self):
        sale = SaleAttributes.objects.create(cashier=self.manager)
        SaleItems.objects.create(sale=sale, sale_item=self.product1, sale_quantity=1)

        self.client.force_login(self.manager)
        response = self._post('delete', [self.product1.pk, self.product2.pk])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['deleted'], 1)
        self.assertIn('error', body)
        self.assertTrue(Product.objects.filter(pk=self.product1.pk).exists())
        self.assertFalse(Product.objects.filter(pk=self.product2.pk).exists())

    def test_no_ids_selected_is_rejected(self):
        self.client.force_login(self.manager)
        response = self._post('set_category', [], 'Bulk B')
        self.assertEqual(response.status_code, 400)


class ProductPositionOrderingTests(TestCase):
    """Barcode.position / ProductSupplier.position -- primary (1) first,
    matching what the grid's Barcode 1 / Supplier (Primary) columns and the
    detail page's "(Primary)" label assume."""

    def setUp(self):
        self.product = Product.objects.create(
            internal_code='P3000001', name='Positioned Widget', sell_price=1, quantity=0
        )
        self.supplier_a = Suppliers.objects.create(name='Supplier A', bulstat='111111111')
        self.supplier_b = Suppliers.objects.create(name='Supplier B', bulstat='222222222')

    def test_barcodes_ordered_by_position(self):
        Barcode.objects.create(code='2222222222225', product=self.product, position=2)
        Barcode.objects.create(code='1111111111116', product=self.product, position=1)

        codes = list(self.product.barcode.values_list('code', flat=True))
        self.assertEqual(codes, ['1111111111116', '2222222222225'])

    def test_suppliers_ordered_by_position(self):
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier_b, position=2)
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier_a, position=1)

        names = [link.supplier.name for link in self.product.product_suppliers.all()]
        self.assertEqual(names, ['Supplier A', 'Supplier B'])

    def test_products_list_grid_exposes_primary_and_alternate_columns(self):
        Barcode.objects.create(code='1111111111116', product=self.product, position=1)
        Barcode.objects.create(code='2222222222225', product=self.product, position=2)
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier_a, position=1)
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier_b, position=2)

        manager = Employee.objects.create_user(
            username='manager9', password='pass12345', role=Employee.MANAGER
        )
        self.client.force_login(manager)
        response = self.client.get(reverse('product_list'))

        row = next(r for r in response.context['products_data'] if r['id'] == self.product.pk)
        self.assertEqual(row['barcode_1'], '1111111111116')
        self.assertEqual(row['barcode_2'], '2222222222225')
        self.assertEqual(row['barcode_3'], '')
        self.assertEqual(row['supplier_1'], 'Supplier A')
        self.assertEqual(row['supplier_2'], 'Supplier B')
        self.assertEqual(row['supplier_3'], '')

    def test_duplicate_supplier_on_same_product_rejected(self):
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier_a, position=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProductSupplier.objects.create(product=self.product, supplier=self.supplier_a, position=2)


class ProductSupplierFormsetTests(TestCase):
    def test_product_create_with_multiple_suppliers_succeeds(self):
        manager = Employee.objects.create_user(
            username='manager10', password='pass12345', role=Employee.MANAGER
        )
        category = Category.objects.create(name='Supplier Formset Category')
        supplier_a = Suppliers.objects.create(name='Formset Supplier A', bulstat='333333333')
        supplier_b = Suppliers.objects.create(name='Formset Supplier B', bulstat='444444444')

        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'P3000002',
            'name': 'Multi Supplier Widget',
            'unit_type': 'piece',
            'sell_price': '3.00',
            'quantity': '0',
            'category': category.pk,
            'barcode-TOTAL_FORMS': '0',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '2',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
            'supplier-0-supplier': supplier_a.pk,
            'supplier-0-position': '1',
            'supplier-1-supplier': supplier_b.pk,
            'supplier-1-position': '2',
            'recipe-TOTAL_FORMS': '0',
            'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0',
            'recipe-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(internal_code='P3000002')
        names = [link.supplier.name for link in product.product_suppliers.all()]
        self.assertEqual(names, ['Formset Supplier A', 'Formset Supplier B'])

    def test_leaving_alternate_supplier_row_blank_does_not_block_save(self):
        """Regression test: the "+ Add another supplier" row's hidden
        `position` field is renumbered by JS the moment the row exists
        (see _supplier_formset.html), even if the user never picks a
        supplier for it. That alone used to make Django treat the still-
        blank row as "changed" and demand a supplier for it -- so setting
        only the primary supplier and leaving the alternate row untouched
        wrongly failed with "This field is required."."""
        manager = Employee.objects.create_user(
            username='manager18', password='pass12345', role=Employee.MANAGER
        )
        category = Category.objects.create(name='Blank Alternate Category')
        supplier_a = Suppliers.objects.create(name='Only Primary Supplier', bulstat='555555555')

        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'P3000003',
            'name': 'Primary Only Widget',
            'unit_type': 'piece',
            'sell_price': '3.00',
            'quantity': '0',
            'category': category.pk,
            'barcode-TOTAL_FORMS': '0',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '2',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
            'supplier-0-supplier': supplier_a.pk,
            'supplier-0-position': '1',
            # Alternate row: "+ Add another supplier" was clicked, but no
            # supplier was ever picked for it -- position still gets
            # renumbered to 2 by JS.
            'supplier-1-supplier': '',
            'supplier-1-position': '2',
            'recipe-TOTAL_FORMS': '0',
            'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0',
            'recipe-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(internal_code='P3000003')
        names = [link.supplier.name for link in product.product_suppliers.all()]
        self.assertEqual(names, ['Only Primary Supplier'])


class FreeInternalCodeTests(TestCase):
    """Matches the exact scenario from the request: codes 4 and 6 taken ->
    first free code (fills the gap) is 1, next free code (past the highest
    used) is 7."""

    def test_no_products_yet(self):
        self.assertEqual(get_free_internal_codes(), (1, 1))

    def test_first_free_fills_the_lowest_gap_next_free_is_past_the_highest(self):
        Product.objects.create(internal_code='4', name='Four', sell_price=1, quantity=0)
        Product.objects.create(internal_code='6', name='Six', sell_price=1, quantity=0)

        self.assertEqual(get_free_internal_codes(), (1, 7))

    def test_non_numeric_codes_are_ignored(self):
        Product.objects.create(internal_code='ABC123', name='Alpha', sell_price=1, quantity=0)
        Product.objects.create(internal_code='2', name='Two', sell_price=1, quantity=0)

        self.assertEqual(get_free_internal_codes(), (1, 3))

    def test_create_and_edit_pages_show_the_suggested_codes(self):
        manager = Employee.objects.create_user(
            username='manager11', password='pass12345', role=Employee.MANAGER
        )
        product4 = Product.objects.create(internal_code='4', name='Four', sell_price=1, quantity=0)
        Product.objects.create(internal_code='6', name='Six', sell_price=1, quantity=0)

        self.client.force_login(manager)

        create_response = self.client.get(reverse('product_create'))
        self.assertEqual(create_response.context['first_free_code'], 1)
        self.assertEqual(create_response.context['next_free_code'], 7)

        edit_response = self.client.get(reverse('product_edit', kwargs={'pk': product4.pk}))
        self.assertEqual(edit_response.context['first_free_code'], 1)
        self.assertEqual(edit_response.context['next_free_code'], 7)


class WeightBasedProductTests(TestCase):
    """`unit_type` + decimal `quantity` -- pieces are still whole numbers
    stored in the same field, weight items can be fractional (kg)."""

    def test_default_unit_type_is_piece(self):
        product = Product.objects.create(internal_code='W000001', name='Default Unit', sell_price=1, quantity=5)
        self.assertEqual(product.unit_type, Product.PIECE)

    def test_weight_product_stores_fractional_quantity(self):
        product = Product.objects.create(
            internal_code='W000002', name='Loose Apples', sell_price=2.5, quantity=0,
            unit_type=Product.WEIGHT,
        )
        product.quantity = Decimal('12.345')
        product.save()
        product.refresh_from_db()
        self.assertEqual(product.quantity, Decimal('12.345'))

    def test_quantity_display_filter_formats_by_unit_type(self):
        self.assertEqual(quantity_display(Decimal('5.000'), Product.PIECE), '5 pcs')
        self.assertEqual(quantity_display(Decimal('2.500'), Product.WEIGHT), '2.500 kg')

    def test_create_form_accepts_weight_unit_type(self):
        manager = Employee.objects.create_user(
            username='manager12', password='pass12345', role=Employee.MANAGER
        )
        category = Category.objects.create(name='Loose Produce')
        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'W000003',
            'name': 'Loose Bananas',
            'unit_type': 'weight',
            'sell_price': '1.80',
            'quantity': '0',
            'category': category.pk,
            'barcode-TOTAL_FORMS': '0',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '0',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
            'recipe-TOTAL_FORMS': '0',
            'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0',
            'recipe-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(internal_code='W000003')
        self.assertEqual(product.unit_type, Product.WEIGHT)

    def test_products_list_grid_includes_unit_type_and_float_quantity(self):
        manager = Employee.objects.create_user(
            username='manager13', password='pass12345', role=Employee.MANAGER
        )
        product = Product.objects.create(
            internal_code='W000004', name='Loose Grapes', sell_price=3, quantity=Decimal('4.250'),
            unit_type=Product.WEIGHT,
        )
        self.client.force_login(manager)
        response = self.client.get(reverse('product_list'))
        row = next(r for r in response.context['products_data'] if r['id'] == product.pk)
        self.assertEqual(row['unit_type'], 'weight')
        self.assertEqual(row['quantity'], 4.25)


class RecipeProductTests(TestCase):
    """A recipe product (e.g. Cappuccino) is made of other products
    (ingredients) instead of holding its own stock."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager14', password='pass12345', role=Employee.MANAGER
        )
        self.milk = Product.objects.create(
            internal_code='R000001', name='Milk', sell_price=2, quantity=Decimal('5.000'),
            unit_type=Product.WEIGHT,
        )
        self.coffee = Product.objects.create(
            internal_code='R000002', name='Coffee Beans', sell_price=20, quantity=Decimal('2.000'),
            unit_type=Product.WEIGHT,
        )
        self.cappuccino = Product.objects.create(
            internal_code='R000003', name='Cappuccino', sell_price=3, quantity=0, is_recipe=True,
        )

    def test_recipe_ingredient_rejects_self_reference(self):
        link = RecipeIngredient(recipe=self.cappuccino, ingredient=self.cappuccino, quantity=Decimal('1.000'))
        with self.assertRaises(ValidationError):
            link.full_clean()

    def test_recipe_ingredient_rejects_nested_recipe(self):
        other_recipe = Product.objects.create(
            internal_code='R000004', name='Other Recipe', sell_price=1, quantity=0, is_recipe=True,
        )
        link = RecipeIngredient(recipe=self.cappuccino, ingredient=other_recipe, quantity=Decimal('1.000'))
        with self.assertRaises(ValidationError):
            link.full_clean()

    def test_recipe_ingredient_form_excludes_recipe_products_from_choices(self):
        other_recipe = Product.objects.create(
            internal_code='R000005', name='Other Recipe 2', sell_price=1, quantity=0, is_recipe=True,
        )
        form = RecipeIngredientForm()
        choices = list(form.fields['ingredient'].queryset)
        self.assertIn(self.milk, choices)
        self.assertNotIn(other_recipe, choices)

    @patch('STORA.products.views.backfill_recipe_ingredient_stock.delay')
    def test_product_create_with_recipe_ingredients_succeeds(self, mock_delay):
        category = Category.objects.create(name='Drinks Recipe Category')
        self.client.force_login(self.manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'R000006',
            'name': 'Latte',
            'unit_type': 'piece',
            'sell_price': '3.50',
            'quantity': '0',
            'category': category.pk,
            'is_recipe': 'on',
            'barcode-TOTAL_FORMS': '0',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '0',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
            'recipe-TOTAL_FORMS': '1',
            'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0',
            'recipe-MAX_NUM_FORMS': '1000',
            'recipe-0-ingredient': self.milk.pk,
            'recipe-0-quantity': '0.200',
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(internal_code='R000006')
        self.assertTrue(product.is_recipe)
        links = list(product.recipe_ingredients.all())
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].ingredient, self.milk)
        self.assertEqual(links[0].quantity, Decimal('0.200'))
        mock_delay.assert_called_once_with(product.pk)

    def test_recalculate_ingredient_cost_sums_ingredient_costs(self):
        self.milk.delivery_price = Decimal('1.80')
        self.milk.save(update_fields=['delivery_price'])
        self.coffee.delivery_price = Decimal('18.00')
        self.coffee.save(update_fields=['delivery_price'])
        RecipeIngredient.objects.create(recipe=self.cappuccino, ingredient=self.milk, quantity=Decimal('0.050'))
        RecipeIngredient.objects.create(recipe=self.cappuccino, ingredient=self.coffee, quantity=Decimal('0.007'))

        self.cappuccino.recalculate_ingredient_cost()

        self.cappuccino.refresh_from_db()
        # 0.050 * 1.80 + 0.007 * 18.00 = 0.09 + 0.126 = 0.216, rounded to
        # the field's 2 decimal places on save.
        self.assertEqual(self.cappuccino.delivery_price, Decimal('0.22'))

    def test_recalculate_ingredient_cost_is_noop_for_non_recipe(self):
        self.milk.delivery_price = Decimal('9.99')
        self.milk.save(update_fields=['delivery_price'])
        self.milk.recalculate_ingredient_cost()
        self.milk.refresh_from_db()
        self.assertEqual(self.milk.delivery_price, Decimal('9.99'))

    @patch('STORA.products.views.backfill_recipe_ingredient_stock.delay')
    def test_create_view_ignores_posted_delivery_price_and_computes_it(self, mock_delay):
        self.milk.delivery_price = Decimal('1.80')
        self.milk.save(update_fields=['delivery_price'])
        category = Category.objects.create(name='Cost Category')
        self.client.force_login(self.manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'R000007',
            'name': 'Cost Test Latte',
            'unit_type': 'piece',
            'sell_price': '3.50',
            # Whatever gets typed/posted here must be overridden by the
            # server-side computed ingredient cost, not trusted as-is.
            'delivery_price': '999.00',
            'quantity': '0',
            'category': category.pk,
            'is_recipe': 'on',
            'barcode-TOTAL_FORMS': '0',
            'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0',
            'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '0',
            'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0',
            'supplier-MAX_NUM_FORMS': '1000',
            'recipe-TOTAL_FORMS': '1',
            'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0',
            'recipe-MAX_NUM_FORMS': '1000',
            'recipe-0-ingredient': self.milk.pk,
            'recipe-0-quantity': '0.200',
        })
        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(internal_code='R000007')
        # 0.200 * 1.80 = 0.36 -- not the posted 999.00.
        self.assertEqual(product.delivery_price, Decimal('0.36'))


class IngredientSearchViewTests(TestCase):
    """Backs the ingredient picker on the recipe formset -- returns JSON so
    the JS never has to render thousands of <option>s into the page."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager15', password='pass12345', role=Employee.MANAGER
        )
        self.raw_materials = Category.objects.create(name='Raw Materials')
        self.drinks = Category.objects.create(name='Drinks')
        self.milk = Product.objects.create(
            internal_code='S000001', name='Milk', sell_price=2, delivery_price=Decimal('1.80'),
            quantity=Decimal('5.000'), unit_type=Product.WEIGHT, category=self.raw_materials,
        )
        self.water = Product.objects.create(
            internal_code='S000002', name='Bottled Water', sell_price=1, category=self.drinks, quantity=0,
        )
        self.cappuccino = Product.objects.create(
            internal_code='S000003', name='Cappuccino', sell_price=3, quantity=0, is_recipe=True,
        )

    def test_requires_login(self):
        response = self.client.get(reverse('ingredient_search'))
        self.assertEqual(response.status_code, 302)

    def test_excludes_recipe_products(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('ingredient_search'))
        ids = [r['id'] for r in response.json()['results']]
        self.assertIn(self.milk.pk, ids)
        self.assertNotIn(self.cappuccino.pk, ids)

    def test_filters_by_category(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('ingredient_search'), {'category': self.raw_materials.pk})
        ids = [r['id'] for r in response.json()['results']]
        self.assertIn(self.milk.pk, ids)
        self.assertNotIn(self.water.pk, ids)

    def test_search_by_name(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('ingredient_search'), {'q': 'Milk'})
        ids = [r['id'] for r in response.json()['results']]
        self.assertIn(self.milk.pk, ids)
        self.assertNotIn(self.water.pk, ids)

    def test_result_includes_delivery_price_for_cost_calculation(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('ingredient_search'), {'q': 'Milk'})
        result = next(r for r in response.json()['results'] if r['id'] == self.milk.pk)
        self.assertEqual(result['delivery_price'], 1.80)


class CategoryCreatePopupTests(TestCase):
    """"+ New category" on the product form opens category_create in a new
    tab (?popup=1) so the in-progress product form isn't lost. On success
    that tab should hand the new category back via postMessage and close,
    not redirect to the categories list like a normal visit would."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager16', password='pass12345', role=Employee.MANAGER
        )

    def test_normal_create_still_redirects_to_category_list(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('category_create'), {'name': 'Snacks'})
        self.assertRedirects(response, reverse('category_list'))

    def test_popup_create_renders_close_script_instead_of_redirecting(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('category_create') + '?popup=1', {'name': 'Raw Materials'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.opener')
        self.assertContains(response, 'category-created')
        category = Category.objects.get(name='Raw Materials')
        self.assertContains(response, str(category.pk))


class SupplierCreatePopupTests(TestCase):
    """Same "+ New X" popup pattern as categories (see CategoryCreatePopupTests),
    applied to "+ New supplier" on the product form's supplier formset."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager17', password='pass12345', role=Employee.MANAGER
        )

    def test_normal_create_still_redirects_to_suppliers_list(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('suppliers_create'), {
            'name': 'Popup Test Supplier A', 'bulstat': '111222333',
        })
        self.assertRedirects(response, reverse('suppliers_list'))

    def test_popup_create_renders_close_script_instead_of_redirecting(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('suppliers_create') + '?popup=1', {
            'name': 'Popup Test Supplier B', 'bulstat': '444555666',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.opener')
        self.assertContains(response, 'supplier-created')
        supplier = Suppliers.objects.get(name='Popup Test Supplier B')
        self.assertContains(response, str(supplier.pk))

