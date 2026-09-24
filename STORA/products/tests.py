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
from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType
from STORA.products.forms import ProductForms, RecipeIngredientForm, TaxGroupForm
from STORA.products.models import (
    Product, Barcode, Category, Suppliers, ProductSupplier, RecipeIngredient, TaxGroup, ProductChangeLog,
)
from STORA.products.scale_barcode import ean13_check_digit, is_valid_ean13, decode_scale_barcode
from STORA.products.templatetags.currency_filters import quantity_display
from STORA.products.views import get_free_internal_codes
from STORA.revisions.models import RevisionAttributes, RevisionItems
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
            'internal_code': 'P10012', 'name': 'No Category Widget', 'unit_type': 'piece',
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
        self.assertContains(response, 'Archive instead')

    def test_archiving_a_product_with_history_hides_it_from_the_list(self):
        manager = Employee.objects.create_user(
            username='manager24', password='pass12345', role=Employee.MANAGER
        )
        product = Product.objects.create(
            internal_code='P1000012', name='Discontinued Thing', sell_price=5, quantity=10
        )
        sale = SaleAttributes.objects.create(cashier=manager)
        SaleItems.objects.create(sale=sale, sale_item=product, sale_quantity=1)

        self.client.force_login(manager)
        response = self.client.post(reverse('product_archive', kwargs={'pk': product.pk}))
        self.assertRedirects(response, reverse('product_list'))

        product.refresh_from_db()
        self.assertTrue(product.is_archived)
        # Still fully intact -- archiving never touches history, unlike delete.
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())
        self.assertEqual(SaleItems.objects.filter(sale_item=product).count(), 1)

        list_response = self.client.get(reverse('product_list'))
        self.assertNotIn(
            product.pk, [row['id'] for row in list_response.context['products_data']],
        )

        shown_response = self.client.get(reverse('product_list'), {'show_archived': '1'})
        self.assertIn(
            product.pk, [row['id'] for row in shown_response.context['products_data']],
        )

    def test_archived_toggle_via_inline_update_endpoint(self):
        manager = Employee.objects.create_user(
            username='manager25', password='pass12345', role=Employee.MANAGER
        )
        product = Product.objects.create(
            internal_code='P1000013', name='Toggle Me', sell_price=5, quantity=0,
        )
        self.client.force_login(manager)

        response = self.client.post(
            reverse('product_inline_update', kwargs={'pk': product.pk}),
            {'field': 'is_archived', 'value': 'true'},
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertTrue(product.is_archived)

        # Reversible -- same endpoint flips it back.
        response = self.client.post(
            reverse('product_inline_update', kwargs={'pk': product.pk}),
            {'field': 'is_archived', 'value': 'false'},
        )
        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertFalse(product.is_archived)


class ProductBarcodeFormsetTests(TestCase):
    def test_product_create_with_multiple_barcodes_succeeds(self):
        manager = Employee.objects.create_user(
            username='manager5', password='pass12345', role=Employee.MANAGER
        )
        category = Category.objects.create(name='Test Category')
        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'P10009',
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
        product = Product.objects.get(internal_code='P10009')
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


class ProductDetailViewTests(TestCase):
    """The read-only detail page -- kept separate from Edit on purpose so
    Cashiers (view_product but not change_product) can still look up a
    product's price/stock while ringing up a sale."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager21', password='pass12345', role=Employee.MANAGER
        )
        self.tax_group = TaxGroup.objects.create(name='Standard', rate=Decimal('20.00'))

    def test_cashier_can_view_but_not_edit(self):
        cashier = Employee.objects.create_user(
            username='cashier21', password='pass12345', role=Employee.CASHIER
        )
        product = Product.objects.create(internal_code='PD000001', name='Cashier View Product', sell_price=1, quantity=0)
        self.client.force_login(cashier)
        detail_response = self.client.get(reverse('product_details', kwargs={'pk': product.pk}))
        self.assertEqual(detail_response.status_code, 200)
        edit_response = self.client.get(reverse('product_edit', kwargs={'pk': product.pk}))
        self.assertEqual(edit_response.status_code, 403)

    def test_without_vat_prices_and_markup_are_computed(self):
        product = Product.objects.create(
            internal_code='PD000002', name='VAT Detail Product', delivery_price=Decimal('6.00'),
            sell_price=Decimal('12.00'), quantity=0, tax_group=self.tax_group,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_details', kwargs={'pk': product.pk}))

        self.assertEqual(response.context['delivery_price_without_vat'], Decimal('5.00'))
        self.assertEqual(response.context['sell_price_without_vat'], Decimal('10.00'))
        self.assertEqual(response.context['markup_percent'], Decimal('100.0'))

    def test_without_vat_prices_absent_when_no_tax_group(self):
        product = Product.objects.create(
            internal_code='PD000003', name='No Tax Group Product', delivery_price=Decimal('6.00'),
            sell_price=Decimal('12.00'), quantity=0,
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_details', kwargs={'pk': product.pk}))

        self.assertNotIn('delivery_price_without_vat', response.context)
        self.assertNotIn('sell_price_without_vat', response.context)
        self.assertEqual(response.context['markup_percent'], Decimal('100.0'))

    def test_markup_absent_when_no_delivery_price(self):
        product = Product.objects.create(internal_code='PD000004', name='No Cost Product', sell_price=5, quantity=0)
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_details', kwargs={'pk': product.pk}))
        self.assertNotIn('markup_percent', response.context)

    def test_recent_activity_shows_latest_sales_and_deliveries(self):
        product = Product.objects.create(internal_code='PD000005', name='Activity Product', sell_price=2, quantity=0)
        supplier = Suppliers.objects.create(name='Detail Supplier', bulstat='999888777')

        sale = SaleAttributes.objects.create(cashier=self.manager)
        SaleItems.objects.create(sale=sale, sale_item=product, sale_quantity=2)
        invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, supplier=supplier, document_type=invoice_type,
            document_date=timezone.localdate(),
        )
        DeliveryItems.objects.create(delivery=delivery, delivery_item=product, delivery_quantity=5, price_at_delivery=1)

        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_details', kwargs={'pk': product.pk}))

        events = response.context['recent_events']
        self.assertEqual(len(events), 2)
        event_types = {e['event_type'] for e in events}
        self.assertEqual(event_types, {'Sale', 'Delivery'})

    def test_recent_activity_capped_at_five(self):
        product = Product.objects.create(internal_code='PD000006', name='Busy Product', sell_price=1, quantity=0)
        sale = SaleAttributes.objects.create(cashier=self.manager)
        for _ in range(7):
            SaleItems.objects.create(sale=sale, sale_item=product, sale_quantity=1)

        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_details', kwargs={'pk': product.pk}))
        self.assertEqual(len(response.context['recent_events']), 5)


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

        invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        delivery = DeliveryAttributes.objects.create(
            receiver=self.manager,
            supplier=self.supplier,
            document_type=invoice_type,
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

    def test_history_shows_write_off_as_negative_event_not_positive_delivery(self):
        # Regression: delivery_quantity is always stored positive (see
        # DeliveryItems.save()) -- a write-off used to show up here as a
        # positive "Delivery" event (stock apparently increasing) instead
        # of the negative reduction it actually was.
        writeoff = DeliveryAttributes.objects.create(
            receiver=self.manager, movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            document_date=self.today,
        )
        DeliveryItems.objects.create(
            delivery=writeoff, delivery_item=self.product, delivery_quantity=Decimal('2'),
        )

        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        events = response.context['events']

        write_off_event = next(e for e in events if e['event_type'] == 'Write-off')
        self.assertEqual(write_off_event['quantity_change'], Decimal('-2'))

        delivery_event = next(e for e in events if e['event_type'] == 'Delivery')
        self.assertEqual(delivery_event['quantity_change'], 10)

    def test_history_filters_out_events_outside_selected_period(self):
        self.client.force_login(self.manager)
        old_start = self.today - timedelta(days=400)
        old_end = self.today - timedelta(days=390)
        response = self.client.get(
            reverse('product_history', kwargs={'pk': self.product.pk}),
            {'start_date': old_start.isoformat(), 'end_date': old_end.isoformat()},
        )
        self.assertEqual(len(response.context['events']), 0)

    def test_history_shows_completed_revision_that_changed_quantity(self):
        revision = RevisionAttributes.objects.create(started_by=self.manager)
        RevisionItems.objects.create(
            revision=revision, product=self.product,
            found_quantity=Decimal('4'), system_quantity_at_start=Decimal('7'),
            price_at_revision=Decimal('1.10'),
        )
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.completed_by = self.manager
        revision.completed_at = timezone.now()
        revision.save(update_fields=['status', 'completed_by', 'completed_at'])

        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        events = response.context['events']
        revision_event = next(e for e in events if e['event_type'] == 'Revised')
        self.assertEqual(revision_event['quantity_change'], Decimal('-3'))
        self.assertEqual(revision_event['cashier'], self.manager)

    def test_history_hides_revision_item_where_found_matched_system(self):
        revision = RevisionAttributes.objects.create(started_by=self.manager)
        RevisionItems.objects.create(
            revision=revision, product=self.product,
            found_quantity=Decimal('7'), system_quantity_at_start=Decimal('7'),
            price_at_revision=Decimal('1.10'),
        )
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.completed_by = self.manager
        revision.completed_at = timezone.now()
        revision.save(update_fields=['status', 'completed_by', 'completed_at'])

        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        events = response.context['events']
        self.assertFalse(any(e['event_type'] == 'Revised' for e in events))

    def test_history_hides_revision_that_is_still_open(self):
        revision = RevisionAttributes.objects.create(started_by=self.manager)
        RevisionItems.objects.create(
            revision=revision, product=self.product,
            found_quantity=Decimal('4'), system_quantity_at_start=Decimal('7'),
            price_at_revision=Decimal('1.10'),
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        events = response.context['events']
        self.assertFalse(any(e['event_type'] == 'Revised' for e in events))


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
        # Creating self.product above now also logs its initial field
        # values (see ProductChangeLogTests) -- cleared here so this
        # class's edit-focused tests start from a clean slate.
        ProductChangeLog.objects.filter(product=self.product).delete()

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

    def test_manager_can_toggle_show_on_pos(self):
        self.client.force_login(self.manager)
        response = self._post('show_on_pos', 'true')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertTrue(self.product.show_on_pos)

        response = self._post('show_on_pos', 'false')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertFalse(self.product.show_on_pos)

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

    def test_inline_edit_logs_change_with_attribution(self):
        self.client.force_login(self.manager)
        self._post('name', 'Renamed Via Grid')
        entry = self.product.change_log.get(field_name='name')
        self.assertEqual(entry.old_value, 'Inline Widget')
        self.assertEqual(entry.new_value, 'Renamed Via Grid')
        self.assertEqual(entry.changed_by, self.manager)


class CategoryTogglePosViewTests(TestCase):
    """The Categories list is still a plain HTML table (not Tabulator), so
    show_on_pos gets one lightweight dedicated toggle endpoint instead of
    the full inline-edit machinery Products uses."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager23', password='pass12345', role=Employee.MANAGER
        )
        self.cashier = Employee.objects.create_user(
            username='cashier22', password='pass12345', role=Employee.CASHIER
        )
        self.category = Category.objects.create(name='Toggle Category', show_on_pos=False)

    def _post(self, value):
        return self.client.post(
            reverse('category_toggle_pos', kwargs={'pk': self.category.pk}), {'value': value},
        )

    def test_cashier_cannot_toggle(self):
        self.client.force_login(self.cashier)
        response = self._post('true')
        self.assertEqual(response.status_code, 403)
        self.category.refresh_from_db()
        self.assertFalse(self.category.show_on_pos)

    def test_manager_can_toggle_on_and_off(self):
        self.client.force_login(self.manager)
        response = self._post('true')
        self.assertEqual(response.status_code, 200)
        self.category.refresh_from_db()
        self.assertTrue(self.category.show_on_pos)

        response = self._post('false')
        self.assertEqual(response.status_code, 200)
        self.category.refresh_from_db()
        self.assertFalse(self.category.show_on_pos)


class CategoryBulkDeleteViewTests(TestCase):
    """Backs the Categories List's own checkbox column + bulk bar Delete
    button -- free multi-select (see category_list.html), one POST deleting
    every checked category by id, mirroring ProductBulkActionView's own
    `delete` action (loop + individual .delete(), not queryset.delete())."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager24', password='pass12345', role=Employee.MANAGER
        )
        self.cashier = Employee.objects.create_user(
            username='cashier23', password='pass12345', role=Employee.CASHIER
        )
        self.category_a = Category.objects.create(name='Bulk Delete A')
        self.category_b = Category.objects.create(name='Bulk Delete B')

    def _post(self, ids):
        return self.client.post(reverse('category_bulk_delete'), {'ids': ids})

    def test_cashier_cannot_bulk_delete(self):
        self.client.force_login(self.cashier)
        response = self._post([self.category_a.pk])
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Category.objects.filter(pk=self.category_a.pk).exists())

    def test_manager_can_bulk_delete(self):
        self.client.force_login(self.manager)
        response = self._post([self.category_a.pk, self.category_b.pk])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deleted'], 2)
        self.assertFalse(Category.objects.filter(pk__in=[self.category_a.pk, self.category_b.pk]).exists())

    def test_no_ids_returns_error(self):
        self.client.force_login(self.manager)
        response = self._post([])
        self.assertEqual(response.status_code, 400)


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

    def test_products_list_grid_exposes_markup_percent(self):
        manager = Employee.objects.create_user(
            username='manager-markup', password='pass12345', role=Employee.MANAGER
        )
        self.client.force_login(manager)

        priced = Product.objects.create(
            internal_code='MRKUP001', name='Markup Product',
            sell_price=Decimal('9.00'), delivery_price=Decimal('5.00'), quantity=1,
        )
        no_cost = Product.objects.create(
            internal_code='MRKUP002', name='No Delivery Price Product',
            sell_price=Decimal('3.00'), delivery_price=None, quantity=1,
        )

        response = self.client.get(reverse('product_list'))
        rows = {r['id']: r for r in response.context['products_data']}

        # (9.00 - 5.00) / 5.00 * 100 = 80.0
        self.assertEqual(rows[priced.pk]['markup_percent'], 80.0)
        # No delivery price -- nothing to compute markup against.
        self.assertIsNone(rows[no_cost.pk]['markup_percent'])

    def test_products_list_grid_exposes_active_price_list(self):
        from datetime import timedelta
        from django.utils import timezone
        from STORA.pricelists.models import PriceList, PriceListRule

        manager = Employee.objects.create_user(
            username='manager-pricelist', password='pass12345', role=Employee.MANAGER
        )
        self.client.force_login(manager)
        today = timezone.localdate()
        price_list = PriceList.objects.create(
            name='Grid Promo', start_date=today - timedelta(days=1), end_date=today + timedelta(days=1),
            created_by=manager,
        )
        PriceListRule.objects.create(
            price_list=price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
            product=self.product, discount_percent=Decimal('10'),
        )

        response = self.client.get(reverse('product_list'))
        row = next(r for r in response.context['products_data'] if r['id'] == self.product.pk)
        self.assertEqual(row['price_list_name'], 'Grid Promo')
        self.assertIn(f'?highlight={self.product.pk}', row['price_list_url'])

    def test_products_list_grid_price_list_blank_when_none_active(self):
        self.client.force_login(Employee.objects.create_user(
            username='manager-no-pricelist', password='pass12345', role=Employee.MANAGER
        ))
        response = self.client.get(reverse('product_list'))
        row = next(r for r in response.context['products_data'] if r['id'] == self.product.pk)
        self.assertEqual(row['price_list_name'], '')

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
            'internal_code': 'P30002',
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
        product = Product.objects.get(internal_code='P30002')
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
            'internal_code': 'P30003',
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
        product = Product.objects.get(internal_code='P30003')
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
            'internal_code': 'W00003',
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
        product = Product.objects.get(internal_code='W00003')
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

    @patch('STORA.core.utils._broker_is_reachable', return_value=True)
    @patch('STORA.products.views.backfill_recipe_ingredient_stock.delay')
    def test_product_create_with_recipe_ingredients_succeeds(self, mock_delay, mock_broker_reachable):
        category = Category.objects.create(name='Drinks Recipe Category')
        self.client.force_login(self.manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'R00006',
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
        product = Product.objects.get(internal_code='R00006')
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
            'internal_code': 'R00007',
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
        product = Product.objects.get(internal_code='R00007')
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


class SupplierSearchViewTests(TestCase):
    """Backs the searchable supplier picker on the delivery form -- same
    reasoning as IngredientSearchViewTests above."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager16', password='pass12345', role=Employee.MANAGER
        )
        self.mitko = Suppliers.objects.create(name='Mitko OOD', bulstat='111111111')
        self.ivan = Suppliers.objects.create(name='Ivan EOOD', bulstat='222222222')

    def test_requires_login(self):
        response = self.client.get(reverse('supplier_search'))
        self.assertEqual(response.status_code, 302)

    def test_search_by_name(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('supplier_search'), {'q': 'Mitko'})
        ids = [r['id'] for r in response.json()['results']]
        self.assertIn(self.mitko.pk, ids)
        self.assertNotIn(self.ivan.pk, ids)

    def test_no_query_returns_all_suppliers(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('supplier_search'))
        ids = [r['id'] for r in response.json()['results']]
        self.assertIn(self.mitko.pk, ids)
        self.assertIn(self.ivan.pk, ids)


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


class CategorySubcategoryTests(TestCase):
    """Category.parent (self-FK) -- a category can optionally belong under
    another one. SET_NULL on delete (mirrors Product.category) and cycle
    prevention in CategoryForm (can't become your own descendant's child)."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager-subcat', password='pass12345', role=Employee.MANAGER
        )
        self.drinks = Category.objects.create(name='Drinks')
        self.sodas = Category.objects.create(name='Sodas', parent=self.drinks)

    def test_subcategory_relationship(self):
        self.assertEqual(self.sodas.parent, self.drinks)
        self.assertIn(self.sodas, self.drinks.subcategories.all())

    def test_get_descendant_ids_includes_grandchildren(self):
        cola = Category.objects.create(name='Cola', parent=self.sodas)
        self.assertCountEqual(self.drinks.get_descendant_ids(), [self.sodas.pk, cola.pk])

    def test_deleting_parent_unparents_subcategory_instead_of_deleting_it(self):
        self.drinks.delete()
        self.sodas.refresh_from_db()
        self.assertIsNone(self.sodas.parent)

    def test_form_rejects_category_as_its_own_parent(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('category_edit', args=[self.drinks.pk]), {
            'name': 'Drinks', 'parent': self.drinks.pk,
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, not redirected
        self.drinks.refresh_from_db()
        self.assertIsNone(self.drinks.parent)

    def test_form_rejects_descendant_as_parent(self):
        # Drinks -> Sodas already exists; making Drinks a child of Sodas
        # would create a cycle.
        self.client.force_login(self.manager)
        response = self.client.post(reverse('category_edit', args=[self.drinks.pk]), {
            'name': 'Drinks', 'parent': self.sodas.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.drinks.refresh_from_db()
        self.assertIsNone(self.drinks.parent)

    def test_form_allows_reparenting_to_an_unrelated_category(self):
        self.client.force_login(self.manager)
        snacks = Category.objects.create(name='Snacks')
        response = self.client.post(reverse('category_edit', args=[self.sodas.pk]), {
            'name': 'Sodas', 'parent': snacks.pk,
        })
        self.assertRedirects(response, reverse('category_list'))
        self.sodas.refresh_from_db()
        self.assertEqual(self.sodas.parent, snacks)

    def test_list_page_shows_parent_name(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('category_list'))
        self.assertContains(response, 'Sodas')
        self.assertContains(response, 'Drinks')


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


class ScaleBarcodeTests(TestCase):
    """Scale (weighing) barcodes: 13 digits total -- a 7 or 8 digit product
    code prefix (registered as Barcode.is_scale_code=True), then a 5 or 4
    digit quantity (grams for a weight product, a plain count for a piece
    one), then an EAN-13 check digit. See STORA/products/scale_barcode.py.

    Both example codes below were computed from the same prefix+quantity
    (2000001 -> qty 00154, or 20000012 -> qty 0154) so a weight-vs-piece
    product resolves the identical scanned digits differently -- the
    quantity codepath is what's under test, not the barcode arithmetic.
    """

    SEVEN_DIGIT_CODE = '2000001001547'   # prefix 2000001, qty 00154, check 7
    EIGHT_DIGIT_CODE = '2000001201541'   # prefix 20000012, qty 0154, check 1

    def test_ean13_check_digit_matches_known_value(self):
        # Verified by hand in the session that added this: odd positions
        # (1,3,5,7,9,11) sum to 8, even (2,4,6,8,10,12) sum to 15*... -> 7.
        self.assertEqual(ean13_check_digit('200000100154'), 7)

    def test_is_valid_ean13_rejects_wrong_check_digit(self):
        self.assertTrue(is_valid_ean13(self.SEVEN_DIGIT_CODE))
        tampered = self.SEVEN_DIGIT_CODE[:-1] + '0'
        self.assertFalse(is_valid_ean13(tampered))

    def test_is_valid_ean13_rejects_non_13_digit_or_non_numeric(self):
        self.assertFalse(is_valid_ean13('123'))
        self.assertFalse(is_valid_ean13('20000010015X7'))

    def test_decode_seven_digit_prefix_weight_product(self):
        milk = Product.objects.create(
            internal_code='SC000001', name='Scale Milk', sell_price=2, quantity=Decimal('5.000'),
            unit_type=Product.WEIGHT,
        )
        Barcode.objects.create(code='2000001', product=milk, is_scale_code=True)

        result = decode_scale_barcode(self.SEVEN_DIGIT_CODE)
        self.assertIsNotNone(result)
        product, quantity = result
        self.assertEqual(product, milk)
        self.assertEqual(quantity, Decimal('0.154'))

    def test_decode_seven_digit_prefix_piece_product(self):
        eggs = Product.objects.create(
            internal_code='SC000002', name='Scale Eggs', sell_price=1, quantity=100,
        )
        Barcode.objects.create(code='2000001', product=eggs, is_scale_code=True)

        product, quantity = decode_scale_barcode(self.SEVEN_DIGIT_CODE)
        self.assertEqual(product, eggs)
        self.assertEqual(quantity, Decimal('154'))

    def test_decode_eight_digit_prefix(self):
        cheese = Product.objects.create(
            internal_code='SC000003', name='Scale Cheese', sell_price=10, quantity=Decimal('3.000'),
            unit_type=Product.WEIGHT,
        )
        Barcode.objects.create(code='20000012', product=cheese, is_scale_code=True)

        product, quantity = decode_scale_barcode(self.EIGHT_DIGIT_CODE)
        self.assertEqual(product, cheese)
        self.assertEqual(quantity, Decimal('0.154'))

    def test_decode_returns_none_for_unregistered_prefix(self):
        self.assertIsNone(decode_scale_barcode(self.SEVEN_DIGIT_CODE))

    def test_decode_returns_none_for_invalid_check_digit(self):
        milk = Product.objects.create(
            internal_code='SC000004', name='Scale Milk 2', sell_price=2, quantity=Decimal('5.000'),
            unit_type=Product.WEIGHT,
        )
        Barcode.objects.create(code='2000001', product=milk, is_scale_code=True)
        tampered = self.SEVEN_DIGIT_CODE[:-1] + '0'
        self.assertIsNone(decode_scale_barcode(tampered))

    def test_regular_barcode_is_not_treated_as_scale_code_by_default(self):
        """is_scale_code defaults to False -- a normal fixed barcode that
        happens to be 13 digits must never get reinterpreted as a scale
        code just because it matches the length."""
        product = Product.objects.create(internal_code='SC000005', name='Normal Product', sell_price=5, quantity=10)
        Barcode.objects.create(code=self.SEVEN_DIGIT_CODE[:7], product=product, is_scale_code=False)

        self.assertIsNone(decode_scale_barcode(self.SEVEN_DIGIT_CODE))

    def test_sale_add_context_exposes_is_scale_code_and_unit_type(self):
        """The scale-barcode decoding happens client-side in sale_add.html
        -- it needs is_scale_code on each barcode and unit_type on each
        product in the JSON blobs the view injects into the page."""
        manager = Employee.objects.create_user(
            username='manager19', password='pass12345', role=Employee.MANAGER
        )
        product = Product.objects.create(
            internal_code='SC000006', name='Scale Context Product', sell_price=2, quantity=Decimal('1.000'),
            unit_type=Product.WEIGHT,
        )
        Barcode.objects.create(code='2000001', product=product, is_scale_code=True)

        self.client.force_login(manager)
        response = self.client.get(reverse('sale_add'))

        barcode_row = next(b for b in response.context['barcodes_data'] if b['code'] == '2000001')
        self.assertTrue(barcode_row['is_scale_code'])
        product_row = next(p for p in response.context['products_data'] if p['id'] == product.pk)
        self.assertEqual(product_row['unit_type'], 'weight')


class TaxGroupTests(TestCase):
    """Tax groups (VAT rates) -- a manageable list like Category, assigned
    to a Product so the create/edit form can compute with/without-VAT
    prices client-side without a separately stored "without VAT" field."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager20', password='pass12345', role=Employee.MANAGER
        )
        self.cashier = Employee.objects.create_user(
            username='cashier20', password='pass12345', role=Employee.CASHIER
        )
        self.standard = TaxGroup.objects.create(name='Standard', rate=Decimal('20.00'))

    def test_str_includes_name_and_rate(self):
        self.assertEqual(str(self.standard), 'Standard (20.00%)')

    def test_rate_must_be_between_0_and_100(self):
        form = TaxGroupForm(data={'name': 'Invalid', 'rate': '150'})
        self.assertFalse(form.is_valid())
        self.assertIn('rate', form.errors)

    def test_cashier_cannot_create_tax_group(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('tax_group_create'))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_list_and_create_tax_group(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('tax_group_list')).status_code, 200)
        response = self.client.post(reverse('tax_group_create'), {'name': 'Reduced', 'rate': '9.00'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TaxGroup.objects.filter(name='Reduced', rate=Decimal('9.00')).exists())

    def test_manager_can_edit_and_delete_tax_group(self):
        self.client.force_login(self.manager)
        edit_response = self.client.post(
            reverse('tax_group_edit', kwargs={'pk': self.standard.pk}), {'name': 'Standard', 'rate': '22.00'}
        )
        self.assertEqual(edit_response.status_code, 302)
        self.standard.refresh_from_db()
        self.assertEqual(self.standard.rate, Decimal('22.00'))

        delete_response = self.client.post(reverse('tax_group_delete', kwargs={'pk': self.standard.pk}))
        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(TaxGroup.objects.filter(pk=self.standard.pk).exists())

    def test_deleting_tax_group_nulls_product_reference(self):
        product = Product.objects.create(
            internal_code='TG000001', name='Tax Group Product', sell_price=1, quantity=0, tax_group=self.standard,
        )
        self.client.force_login(self.manager)
        self.client.post(reverse('tax_group_delete', kwargs={'pk': self.standard.pk}))
        product.refresh_from_db()
        self.assertIsNone(product.tax_group)

    def test_popup_create_renders_close_script_with_rate(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('tax_group_create') + '?popup=1', {'name': 'Zero-rated', 'rate': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.opener')
        self.assertContains(response, 'tax-group-created')
        tax_group = TaxGroup.objects.get(name='Zero-rated')
        self.assertContains(response, str(tax_group.pk))

    def test_product_create_and_edit_pages_expose_tax_group_rates(self):
        self.client.force_login(self.manager)
        create_response = self.client.get(reverse('product_create'))
        self.assertEqual(create_response.context['tax_group_rates'], {str(self.standard.pk): '20.00'})

        product = Product.objects.create(internal_code='TG000002', name='Rates Context Product', sell_price=1, quantity=0)
        edit_response = self.client.get(reverse('product_edit', kwargs={'pk': product.pk}))
        self.assertEqual(edit_response.context['tax_group_rates'], {str(self.standard.pk): '20.00'})

    def test_product_can_be_created_with_tax_group(self):
        category = Category.objects.create(name='VAT Test Category')
        self.client.force_login(self.manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'TG0003',
            'name': 'VAT Test Product',
            'unit_type': 'piece',
            'delivery_price': '1.00',
            'sell_price': '1.50',
            'quantity': '0',
            'category': category.pk,
            'tax_group': self.standard.pk,
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
        product = Product.objects.get(internal_code='TG0003')
        self.assertEqual(product.tax_group, self.standard)


class ProductChangeLogTests(TestCase):
    """"Did someone touch this product?" -- an entry per changed tracked
    field, attributed to whoever made the edit. Populated by the pre_save/
    post_save signals in models.py, not by the views directly."""

    def setUp(self):
        self.manager = Employee.objects.create_user(
            username='manager22', password='pass12345', role=Employee.MANAGER
        )
        self.category = Category.objects.create(name='Change Log Category')
        self.product = Product.objects.create(
            internal_code='CL000001', name='Original Name', sell_price=Decimal('2.00'), quantity=5,
            category=self.category,
        )
        # Creating self.product above now also logs its initial field
        # values (see test_creating_a_product_directly_via_the_orm_logs_
        # initial_values for that behavior specifically, tested on its own
        # product) -- cleared here so every other test in this class (all
        # about EDITS) starts from a clean slate.
        ProductChangeLog.objects.filter(product=self.product).delete()

    def _edit_post_data(self, **overrides):
        data = {
            'internal_code': self.product.internal_code,
            'name': self.product.name,
            'unit_type': 'piece',
            'sell_price': str(self.product.sell_price),
            'category': self.category.pk,
            'barcode-TOTAL_FORMS': '0', 'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0', 'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '0', 'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0', 'supplier-MAX_NUM_FORMS': '1000',
            'recipe-TOTAL_FORMS': '0', 'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0', 'recipe-MAX_NUM_FORMS': '1000',
        }
        data.update(overrides)
        return data

    def test_creating_a_product_directly_via_the_orm_logs_initial_values(self):
        # No _changed_by set (same as any direct ORM creation outside a
        # view) -- logs each tracked field with a blank old_value,
        # changed_by left None.
        product = Product.objects.create(
            internal_code='CL000099', name='Freshly Made', sell_price=Decimal('3.00'), quantity=0,
        )
        entries = {e.field_name: e for e in product.change_log.all()}
        self.assertEqual(entries['name'].old_value, '')
        self.assertEqual(entries['name'].new_value, 'Freshly Made')
        self.assertIsNone(entries['name'].changed_by)
        self.assertNotIn('quantity', entries)  # never tracked, creation or not

    def test_creating_a_product_via_the_view_attributes_it_to_the_creator(self):
        manager = Employee.objects.create_user(
            username='manager23', password='pass12345', role=Employee.MANAGER
        )
        self.client.force_login(manager)
        response = self.client.post(reverse('product_create'), {
            'internal_code': 'CL0002',
            'name': 'Freshly Created Product',
            'unit_type': 'piece',
            'sell_price': '4.50',
            'quantity': '0',
            'category': self.category.pk,
            'barcode-TOTAL_FORMS': '0', 'barcode-INITIAL_FORMS': '0',
            'barcode-MIN_NUM_FORMS': '0', 'barcode-MAX_NUM_FORMS': '1000',
            'supplier-TOTAL_FORMS': '0', 'supplier-INITIAL_FORMS': '0',
            'supplier-MIN_NUM_FORMS': '0', 'supplier-MAX_NUM_FORMS': '1000',
            'recipe-TOTAL_FORMS': '0', 'recipe-INITIAL_FORMS': '0',
            'recipe-MIN_NUM_FORMS': '0', 'recipe-MAX_NUM_FORMS': '1000',
        })
        self.assertEqual(response.status_code, 302)

        created = Product.objects.get(internal_code='CL0002')
        entry = created.change_log.get(field_name='name')
        self.assertEqual(entry.old_value, '')
        self.assertEqual(entry.new_value, 'Freshly Created Product')
        self.assertEqual(entry.changed_by, manager)

    def test_editing_name_logs_old_and_new_value_with_attribution(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('product_edit', kwargs={'pk': self.product.pk}),
            self._edit_post_data(name='Renamed Product'),
        )
        self.assertEqual(response.status_code, 302)

        entry = self.product.change_log.get(field_name='name')
        self.assertEqual(entry.old_value, 'Original Name')
        self.assertEqual(entry.new_value, 'Renamed Product')
        self.assertEqual(entry.changed_by, self.manager)

    def test_editing_multiple_fields_logs_one_entry_each(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse('product_edit', kwargs={'pk': self.product.pk}),
            self._edit_post_data(name='Multi Change Product', sell_price='9.99'),
        )
        self.assertEqual(response.status_code, 302)

        fields_changed = set(self.product.change_log.values_list('field_name', flat=True))
        self.assertEqual(fields_changed, {'name', 'sell_price'})

    def test_resubmitting_identical_values_logs_nothing(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('product_edit', kwargs={'pk': self.product.pk}), self._edit_post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.product.change_log.count(), 0)

    def test_quantity_changes_are_not_logged(self):
        """quantity already has its own history via SaleItems/DeliveryItems
        (see ProductHistoryView) -- it's deliberately excluded here."""
        self.client.force_login(self.manager)
        self.client.post(reverse('product_edit', kwargs={'pk': self.product.pk}), self._edit_post_data(quantity='9999'))
        self.assertFalse(self.product.change_log.filter(field_name='quantity').exists())

    def test_history_view_exposes_changes_in_date_range(self):
        self.client.force_login(self.manager)
        self.client.post(
            reverse('product_edit', kwargs={'pk': self.product.pk}),
            self._edit_post_data(name='Visible In History'),
        )
        response = self.client.get(reverse('product_history', kwargs={'pk': self.product.pk}))
        change_rows = [c for c in response.context['changes_data'] if c['field_name'] == 'name']
        self.assertEqual(len(change_rows), 1)
        self.assertEqual(change_rows[0]['old_value'], 'Original Name')
        self.assertEqual(change_rows[0]['new_value'], 'Visible In History')
        self.assertEqual(change_rows[0]['changed_by'], str(self.manager))
