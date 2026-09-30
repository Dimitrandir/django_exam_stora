import io
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import openpyxl
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from STORA.deliveries.excel_import import parse_delivery_import
from STORA.deliveries.models import (
    DeliveryAttributes, DeliveryItems, DeliveryItemRemoval, DocumentType, ScrapReason, Suppliers,
)
from STORA.products.models import Barcode, Category, Product, ProductSupplier, TaxGroup


User = get_user_model()


class DeliveryViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='receiver',
            password='pass12345',
            role=User.WAREHOUSE,
        )
        self.client.login(username='receiver', password='pass12345')

    def test_delivery_add_page_loads(self):
        response = self.client.get(reverse('delivery_add'))
        self.assertEqual(response.status_code, 200)

    def test_delivery_add_page_uses_template(self):
        response = self.client.get(reverse('delivery_add'))
        self.assertTemplateUsed(response, 'deliveries/delivery_add.html')


class DeliveryApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            password='pass12345',
        )
        self.client.login(username='testuser', password='pass12345')

        self.product = Product.objects.create(
            internal_code='P0000002',
            name='Delivery Product',
            delivery_price=1.25,
            sell_price=2.25,
            quantity=5,
        )

        self.supplier = Suppliers.objects.create(
            name='Supplier Ltd',
        )
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')

        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user,
            supplier=self.supplier,
            document_type=self.invoice_type,
            document_number='INV-001',
            document_date='2026-04-20',
        )

        DeliveryItems.objects.create(
            delivery=self.delivery,
            delivery_item=self.product,
            delivery_quantity=4,
            price_at_delivery=1.25,
            total_price_row=5.00,
        )

    def test_deliveries_api_returns_200_for_authenticated_user(self):
        response = self.client.get(reverse('api_deliveries'))
        self.assertEqual(response.status_code, 200)

    def test_deliveries_api_returns_delivery_data(self):
        response = self.client.get(reverse('api_deliveries'))
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['id'], self.delivery.id)

    def test_deliveries_api_requires_authentication(self):
        self.client.logout()
        response = self.client.get(reverse('api_deliveries'))
        self.assertIn(response.status_code, (302, 401, 403))


class DeliveryItemDecimalQuantityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='receiver2', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Weight Supplier Ltd', bulstat='555555555')
        self.product = Product.objects.create(
            internal_code='P0000006', name='Loose Tomatoes', sell_price=2.0, quantity=Decimal('0.000'),
            unit_type=Product.WEIGHT,
        )
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-002', document_date='2026-04-20',
        )

    def test_delivery_item_accepts_fractional_quantity_for_weight_products(self):
        # price_at_delivery must be a Decimal (or string), not a bare float
        # -- Decimal * float raises TypeError. A real form submission always
        # goes through DecimalField.to_python() first so this only bites
        # code that builds the model directly, as here.
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('18.750'), price_at_delivery=Decimal('1.50'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('18.750'))

    def test_explicit_zero_price_is_not_overwritten_by_catalog_price(self):
        # `if not self.price_at_delivery:` used to treat an explicit 0 the
        # same as "not given" (Decimal('0') is falsy) and silently replaced
        # it with product.delivery_price. Here that's None (never delivered
        # before), so the multiplication crashed with
        # `TypeError: unsupported operand type(s) for *: 'decimal.Decimal' and 'NoneType'`.
        self.assertIsNone(self.product.delivery_price)
        item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('0.00'),
        )
        self.assertEqual(item.price_at_delivery, Decimal('0.00'))
        self.assertEqual(item.total_price_row, Decimal('0.00'))

    def test_missing_price_with_no_catalog_price_falls_back_to_zero(self):
        # price_at_delivery not given at all, and the product has no
        # delivery_price yet either -- must not crash, should default to 0.
        self.assertIsNone(self.product.delivery_price)
        item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'),
        )
        self.assertEqual(item.price_at_delivery, Decimal('0.00'))
        self.assertEqual(item.total_price_row, Decimal('0.00'))


class DeliveryStockAdjustmentTests(TestCase):
    """DeliveryItems.save() used to unconditionally do
    `product.quantity += delivery_quantity` on every save -- including when
    editing an already-saved item, which double-counted stock, and nothing
    ever gave stock back when an item/delivery was deleted. These pin down
    the delta-based fix (mirrors SaleItems)."""

    def setUp(self):
        self.user = User.objects.create_user(username='receiver3', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Stock Supplier Ltd', bulstat='111111111')
        self.product = Product.objects.create(
            internal_code='P0000007', name='Canned Beans', delivery_price=Decimal('1.00'),
            sell_price=Decimal('2.00'), quantity=Decimal('10.000'),
        )
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-003', document_date='2026-04-20',
        )

    def test_editing_quantity_applies_only_the_delta(self):
        item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('15.000'))  # 10 + 5

        item.delivery_quantity = Decimal('8.000')
        item.save()
        self.product.refresh_from_db()
        # Only the +3 delta should apply, not another full +8.
        self.assertEqual(self.product.quantity, Decimal('18.000'))

    def test_resaving_without_changing_quantity_does_not_double_count(self):
        item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        item.price_at_delivery = Decimal('1.20')
        item.save()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('15.000'))  # still just 10 + 5

    def test_deleting_delivery_item_restores_stock(self):
        item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        item.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('10.000'))

    def test_deleting_whole_delivery_restores_stock_for_all_items(self):
        other_product = Product.objects.create(
            internal_code='P0000008', name='Rice Bag', delivery_price=Decimal('2.00'),
            sell_price=Decimal('3.00'), quantity=Decimal('4.000'),
        )
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=other_product,
            delivery_quantity=Decimal('2.000'), price_at_delivery=Decimal('2.00'),
        )
        self.delivery.delete()
        self.product.refresh_from_db()
        other_product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('10.000'))
        self.assertEqual(other_product.quantity, Decimal('4.000'))

    def test_deleting_whole_delivery_does_not_create_removal_records(self):
        # Regression test: Django's CASCADE deletes the child DeliveryItems
        # rows BEFORE the parent DeliveryAttributes row, so a naive
        # `except DeliveryAttributes.DoesNotExist` inside the post_delete
        # signal never actually fires for this case -- confirmed live, this
        # used to write a DeliveryItemRemoval for every item in a deleted
        # delivery, which permanently PROTECTs those products from ever
        # being hard-deleted again (DeliveryItemRemoval.product is
        # on_delete=PROTECT). A whole-delivery delete should leave no trace
        # at all, same as if the delivery had never been entered.
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        self.delivery.delete()
        self.assertEqual(DeliveryItemRemoval.objects.filter(product=self.product).count(), 0)
        # And the product must still be hard-deletable -- nothing left
        # behind that would PROTECT it.
        self.product.delete()
        self.assertFalse(Product.objects.filter(pk=self.product.pk).exists())

    def test_delivery_total_amount_recalculates_automatically(self):
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.total_amount, Decimal('5.00'))

        item2 = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('3.00'),
        )
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.total_amount, Decimal('8.00'))  # 5 + 3

        item2.delete()
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.total_amount, Decimal('5.00'))


class DeliverySupplierLinkTests(TestCase):
    """A real delivery used to never touch Product.supplier at all -- a
    product could be delivered from a supplier over and over and never show
    up linked to them anywhere (Product edit form, bulk pickers, ...).
    DeliveryItems.save() now auto-links the delivery's supplier: primary if
    the product had none, otherwise appended as the next alternate."""

    def setUp(self):
        self.user = User.objects.create_user(username='receiver4', password='pass12345')
        self.supplier = Suppliers.objects.create(name='First Supplier Ltd', bulstat='121212121')
        self.other_supplier = Suppliers.objects.create(name='Second Supplier Ltd', bulstat='343434343')
        self.product = Product.objects.create(
            internal_code='P0000009', name='Olive Oil', delivery_price=Decimal('3.00'),
            sell_price=Decimal('5.00'), quantity=Decimal('0.000'),
        )
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier,
            document_number='INV-004', document_date='2026-04-20',
        )

    def test_first_delivery_links_supplier_as_primary(self):
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        links = list(ProductSupplier.objects.filter(product=self.product).values_list('supplier_id', 'position'))
        self.assertEqual(links, [(self.supplier.pk, 1)])

    def test_second_delivery_from_a_different_supplier_becomes_an_alternate(self):
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        other_delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.other_supplier,
            document_number='INV-005', document_date='2026-04-21',
        )
        DeliveryItems.objects.create(
            delivery=other_delivery, delivery_item=self.product,
            delivery_quantity=Decimal('2.000'), price_at_delivery=Decimal('3.00'),
        )
        links = list(ProductSupplier.objects.filter(product=self.product).order_by('position')
                     .values_list('supplier_id', 'position'))
        self.assertEqual(links, [(self.supplier.pk, 1), (self.other_supplier.pk, 2)])

    def test_repeat_delivery_from_the_same_supplier_does_not_duplicate(self):
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('3.00'),
        )
        self.assertEqual(ProductSupplier.objects.filter(product=self.product).count(), 1)

    def test_product_that_already_has_a_primary_supplier_keeps_it(self):
        existing_supplier = Suppliers.objects.create(name='Existing Primary Ltd', bulstat='565656565')
        ProductSupplier.objects.create(product=self.product, supplier=existing_supplier, position=1)

        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        links = list(ProductSupplier.objects.filter(product=self.product).order_by('position')
                     .values_list('supplier_id', 'position'))
        self.assertEqual(links, [(existing_supplier.pk, 1), (self.supplier.pk, 2)])

    def test_write_off_does_not_link_a_supplier(self):
        write_off = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            document_number='WO-TEST', document_date='2026-04-20',
        )
        self.product.quantity = Decimal('5.000')
        self.product.save(update_fields=['quantity'])
        DeliveryItems.objects.create(
            delivery=write_off, delivery_item=self.product,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('3.00'),
        )
        self.assertFalse(ProductSupplier.objects.filter(product=self.product).exists())

    def test_deleting_the_only_delivery_item_retracts_the_auto_link(self):
        item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        self.assertTrue(ProductSupplier.objects.filter(product=self.product, supplier=self.supplier).exists())

        item.delete()
        self.assertFalse(ProductSupplier.objects.filter(product=self.product, supplier=self.supplier).exists())

    def test_deleting_the_whole_delivery_does_not_retract_the_auto_link(self):
        # Deleting the WHOLE delivery (not just correcting one line) is a
        # bigger, deliberate "undo this entire delivery" action -- the
        # product/supplier relationship it established shouldn't be
        # retroactively erased just because the paperwork got deleted.
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        self.assertTrue(ProductSupplier.objects.filter(product=self.product, supplier=self.supplier).exists())

        self.delivery.delete()
        self.assertTrue(ProductSupplier.objects.filter(product=self.product, supplier=self.supplier).exists())

    def test_deleting_one_of_two_deliveries_from_the_same_supplier_keeps_the_link(self):
        item1 = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('3.00'),
        )
        second_delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier,
            document_number='INV-006', document_date='2026-04-22',
        )
        DeliveryItems.objects.create(
            delivery=second_delivery, delivery_item=self.product,
            delivery_quantity=Decimal('2.000'), price_at_delivery=Decimal('3.00'),
        )

        item1.delete()
        # The other delivery from the same supplier is still there -- the
        # link is still justified, shouldn't be retracted.
        self.assertTrue(ProductSupplier.objects.filter(product=self.product, supplier=self.supplier).exists())

    def test_manually_added_supplier_link_is_never_retracted(self):
        ProductSupplier.objects.create(
            product=self.product, supplier=self.other_supplier, position=1, linked_from_delivery=False,
        )
        # A delivery from the SAME (manually-linked) supplier -- since it
        # already existed, DeliveryItems.save() doesn't touch/flag it.
        delivery_from_other = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.other_supplier,
            document_number='INV-007', document_date='2026-04-23',
        )
        item = DeliveryItems.objects.create(
            delivery=delivery_from_other, delivery_item=self.product,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('3.00'),
        )
        item.delete()
        # Manually-added link (linked_from_delivery=False) survives even
        # though this was the only delivery from that supplier.
        self.assertTrue(ProductSupplier.objects.filter(product=self.product, supplier=self.other_supplier).exists())


class DeliveryPermissionTests(TestCase):
    """Deliveries views used to only check @login_required -- any logged-in
    employee (even a Cashier) could add/edit/delete a delivery. These pin
    down the same Cashier=view-only / Warehouse=add+change / Manager=all
    model already enforced for products."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='cashier1', password='pass12345', role=User.CASHIER)
        self.warehouse = User.objects.create_user(username='warehouse1', password='pass12345', role=User.WAREHOUSE)
        self.manager = User.objects.create_user(username='manager1', password='pass12345', role=User.MANAGER)
        self.supplier = Suppliers.objects.create(name='Perm Supplier', bulstat='999999999')
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-010', document_date='2026-04-20',
        )

    def test_cashier_can_view_but_not_add_delivery(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse('deliveries_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('delivery_details', args=[self.delivery.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('delivery_add')).status_code, 403)
        self.assertEqual(self.client.get(reverse('delivery_edit', args=[self.delivery.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('delivery_delete', args=[self.delivery.pk])).status_code, 403)

    def test_warehouse_can_add_and_edit_but_not_delete_delivery(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('delivery_add')).status_code, 200)
        self.assertEqual(self.client.get(reverse('delivery_edit', args=[self.delivery.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('delivery_delete', args=[self.delivery.pk])).status_code, 403)

    def test_manager_can_do_everything(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('delivery_add')).status_code, 200)
        self.assertEqual(self.client.get(reverse('delivery_edit', args=[self.delivery.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('delivery_delete', args=[self.delivery.pk])).status_code, 200)

    def test_logged_out_user_is_redirected_to_login_not_403(self):
        response = self.client.get(reverse('delivery_add'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)


class DocumentTypeCRUDTests(TestCase):
    """Document types are a short, rarely-changed reference list -- Manager
    only can add/rename entries (no shortcut link from the delivery form
    either); Warehouse/Cashier can only pick from what already exists."""

    def setUp(self):
        self.manager = User.objects.create_user(username='manager2', password='pass12345', role=User.MANAGER)
        self.warehouse = User.objects.create_user(username='warehouse2', password='pass12345', role=User.WAREHOUSE)
        self.cashier = User.objects.create_user(username='cashier2', password='pass12345', role=User.CASHIER)
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')

    def test_manager_can_list_and_create(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('document_type_list')).status_code, 200)
        response = self.client.post(reverse('document_type_create'), {'name': 'Credit Note'})
        self.assertRedirects(response, reverse('document_type_list'))
        self.assertTrue(DocumentType.objects.filter(name='Credit Note').exists())

    def test_warehouse_can_view_but_not_create(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('document_type_list')).status_code, 200)
        response = self.client.get(reverse('document_type_create'))
        self.assertEqual(response.status_code, 403)

    def test_cashier_cannot_create(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('document_type_create'))
        self.assertEqual(response.status_code, 403)

    def test_delivery_form_has_no_new_document_type_shortcut(self):
        # Document types are managed from their own page only -- no popup
        # shortcut cluttering the delivery form.
        self.client.force_login(self.warehouse)
        response = self.client.get(reverse('delivery_add'))
        self.assertContains(response, 'Invoice')
        self.assertNotContains(response, 'New document type')

    def test_delivery_form_uses_document_type_choices(self):
        self.client.force_login(self.warehouse)
        response = self.client.get(reverse('delivery_add'))
        self.assertContains(response, 'Invoice')


class DeliveryFormSubmissionTests(TestCase):
    """End-to-end POST tests for the Tabulator-driven items grid in
    _delivery_items_table.html -- the visible table is pure JS/Tabulator
    state, rebuilt into plain `items-<n>-<field>` hidden inputs right
    before submit (see rebuildHiddenInputs() in that template). These tests
    post that exact shape directly, standing in for what the JS produces,
    to prove the view/form/formset wiring on the other end is correct."""

    def setUp(self):
        self.warehouse = User.objects.create_user(username='warehouse3', password='pass12345', role=User.WAREHOUSE)
        self.supplier = Suppliers.objects.create(name='Submission Supplier', bulstat='333333333')
        self.product = Product.objects.create(
            internal_code='P0000009', name='Flour 1kg', delivery_price=Decimal('1.50'),
            sell_price=Decimal('2.50'), quantity=Decimal('0.000'),
        )
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.client.force_login(self.warehouse)

    def _general_info(self, **overrides):
        data = {
            'supplier': self.supplier.pk,
            'time_of_delivery': '2026-04-20 10:00:00',
            'document_type': self.invoice_type.pk,
            'document_number': 'INV-100',
            'document_date': '2026-04-20',
        }
        data.update(overrides)
        return data

    def test_creating_delivery_with_one_item_via_rebuilt_hidden_inputs(self):
        data = self._general_info()
        data.update({
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '10.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '15.00',
            'items-0-DELETE': '',
        })
        response = self.client.post(reverse('delivery_add'), data)
        self.assertRedirects(response, reverse('delivery_add'))

        delivery = DeliveryAttributes.objects.get(document_number='INV-100')
        self.assertEqual(delivery.items.count(), 1)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('10.000'))

    def test_creating_delivery_with_expiry_date_on_item(self):
        data = self._general_info(document_number='INV-103')
        data.update({
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '3.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '4.50',
            'items-0-expiry_date': '2026-12-31',
            'items-0-DELETE': '',
        })
        response = self.client.post(reverse('delivery_add'), data)
        self.assertRedirects(response, reverse('delivery_add'))

        item = DeliveryItems.objects.get(delivery__document_number='INV-103')
        self.assertEqual(item.expiry_date.isoformat(), '2026-12-31')

    def test_editing_existing_item_quantity_applies_delta_not_full_amount(self):
        delivery = DeliveryAttributes.objects.create(
            receiver=self.warehouse, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-101', document_date='2026-04-20',
        )
        item = DeliveryItems.objects.create(
            delivery=delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.50'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('5.000'))

        data = self._general_info(document_number='INV-101')
        data.update({
            'receiver': self.warehouse.pk,
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '1',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': item.pk,
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '9.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '13.50',
            'items-0-DELETE': '',
        })
        response = self.client.post(reverse('delivery_edit', args=[delivery.pk]), data)
        self.assertRedirects(response, reverse('delivery_details', args=[delivery.pk]))

        self.product.refresh_from_db()
        # Only the +4 delta should apply (5 -> 9), not another +9 on top.
        self.assertEqual(self.product.quantity, Decimal('9.000'))

    def test_marking_existing_item_deleted_restores_stock_and_removes_it(self):
        delivery = DeliveryAttributes.objects.create(
            receiver=self.warehouse, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-102', document_date='2026-04-20',
        )
        other_product = Product.objects.create(
            internal_code='P0000010', name='Sugar 1kg', delivery_price=Decimal('2.00'),
            sell_price=Decimal('3.00'), quantity=Decimal('0.000'),
        )
        item = DeliveryItems.objects.create(
            delivery=delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.50'),
        )
        kept_item = DeliveryItems.objects.create(
            delivery=delivery, delivery_item=other_product,
            delivery_quantity=Decimal('2.000'), price_at_delivery=Decimal('2.00'),
        )

        data = self._general_info(document_number='INV-102')
        data.update({
            'receiver': self.warehouse.pk,
            'items-TOTAL_FORMS': '2',
            'items-INITIAL_FORMS': '2',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            # Deleted row -- resent unchanged, just flagged DELETE=on (this
            # is what rebuildHiddenInputs() does for a removed existing row).
            'items-0-id': item.pk,
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '5.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '7.50',
            'items-0-DELETE': 'on',
            # Untouched kept row.
            'items-1-id': kept_item.pk,
            'items-1-delivery_item': other_product.pk,
            'items-1-delivery_quantity': '2.000',
            'items-1-price_at_delivery': '2.00',
            'items-1-total_price_row': '4.00',
            'items-1-DELETE': '',
        })
        response = self.client.post(reverse('delivery_edit', args=[delivery.pk]), data)
        self.assertRedirects(response, reverse('delivery_details', args=[delivery.pk]))

        self.assertFalse(DeliveryItems.objects.filter(pk=item.pk).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('0.000'))  # 5 added, then restored
        other_product.refresh_from_db()
        self.assertEqual(other_product.quantity, Decimal('2.000'))  # untouched

        # The removed row leaves an audit trail (see DeliveryItemRemoval) --
        # attributed to whoever submitted the edit, since self.warehouse is
        # logged in for this whole test class.
        removal = DeliveryItemRemoval.objects.get(product=self.product)
        self.assertEqual(removal.delivery_quantity, Decimal('5.000'))
        self.assertEqual(removal.supplier, self.supplier)
        self.assertEqual(removal.removed_by, self.warehouse)
        # The untouched row wasn't removed -- no audit record for it.
        self.assertFalse(DeliveryItemRemoval.objects.filter(product=other_product).exists())

    def test_replacing_an_item_shows_up_in_product_history_after_removal(self):
        # The exact scenario reported live: edit an existing delivery,
        # swap one product for a different one on the same row. Once
        # DeliveryItems.delete() runs, that row is gone from the DB
        # entirely -- ProductHistoryView has nothing left to read UNLESS
        # DeliveryItemRemoval captured it first.
        delivery = DeliveryAttributes.objects.create(
            receiver=self.warehouse, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-104', document_date='2026-04-20', time_of_delivery='2026-04-20 09:00:00',
        )
        replaced_product = self.product
        new_product = Product.objects.create(
            internal_code='P0000011', name='Replacement Product', delivery_price=Decimal('1.00'),
            sell_price=Decimal('2.00'), quantity=Decimal('0.000'),
        )
        item = DeliveryItems.objects.create(
            delivery=delivery, delivery_item=replaced_product,
            delivery_quantity=Decimal('3.000'), price_at_delivery=Decimal('1.50'),
        )

        data = self._general_info(document_number='INV-104')
        data.update({
            'receiver': self.warehouse.pk,
            'items-TOTAL_FORMS': '2',
            'items-INITIAL_FORMS': '1',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': item.pk,
            'items-0-delivery_item': replaced_product.pk,
            'items-0-delivery_quantity': '3.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '4.50',
            'items-0-DELETE': 'on',
            'items-1-id': '',
            'items-1-delivery_item': new_product.pk,
            'items-1-delivery_quantity': '1.000',
            'items-1-price_at_delivery': '1.00',
            'items-1-total_price_row': '1.00',
            'items-1-DELETE': '',
        })
        response = self.client.post(reverse('delivery_edit', args=[delivery.pk]), data)
        self.assertRedirects(response, reverse('delivery_details', args=[delivery.pk]))

        history_response = self.client.get(
            reverse('product_history', kwargs={'pk': replaced_product.pk}),
            {'start_date': '2026-04-01', 'end_date': '2026-04-30'},
        )
        removal_events = [e for e in history_response.context['events'] if e['event_type'] == 'Delivery (removed)']
        self.assertEqual(len(removal_events), 1)
        self.assertEqual(removal_events[0]['quantity_change'], Decimal('3.000'))
        self.assertEqual(removal_events[0]['cashier'], self.warehouse)

    def test_invalid_submission_with_blank_supplier_does_not_500(self):
        # Suppliers.objects.filter(pk='') raises ValueError (not a clean
        # "no match"), since '' isn't a valid int -- the re-render path that
        # resolves the posted/drafted supplier id back to a display name
        # must guard against that instead of crashing the whole request.
        data = self._general_info(supplier='', document_date='')  # blank date -> form invalid
        data.update({
            'items-TOTAL_FORMS': '0', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
        })
        response = self.client.post(reverse('delivery_add'), data)
        self.assertEqual(response.status_code, 200)


class DeliveryDetailViewItemsDataTests(TestCase):
    """Delivery Details used to show a plain 5-column table. Now it's the
    same rich read-only Tabulator shape as the items grid on Add/Edit
    (VAT-exclusive price, current Sell Price/Markup %, expiry date)."""

    def setUp(self):
        self.manager = User.objects.create_user(username='manager4', password='pass12345', role=User.MANAGER)
        self.supplier = Suppliers.objects.create(name='Detail Supplier', bulstat='777777777')
        self.tax_group = TaxGroup.objects.create(name='Detail 20', rate=Decimal('20'))
        self.product = Product.objects.create(
            internal_code='DET001', name='Detail Product', delivery_price=Decimal('12.00'),
            sell_price=Decimal('20.00'), quantity=Decimal('5.000'), tax_group=self.tax_group,
        )
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.manager, supplier=self.supplier, document_type=self.invoice_type,
            document_number='DET-1', document_date='2026-04-20',
        )
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('12.00'),
            expiry_date='2026-12-31',
        )
        self.client.force_login(self.manager)

    def test_items_data_includes_vat_and_sell_price_breakdown(self):
        response = self.client.get(reverse('delivery_details', args=[self.delivery.pk]))
        row = response.context['delivery_items_data'][0]
        self.assertEqual(row['internal_code'], 'DET001')
        self.assertEqual(row['price_at_delivery'], 12.0)
        self.assertEqual(row['price_without_vat'], 10.0)
        self.assertEqual(row['total_price_row'], 60.0)
        self.assertEqual(row['total_without_vat'], 50.0)
        self.assertEqual(row['sell_price'], 20.0)
        self.assertEqual(row['markup_percent'], 66.7)
        self.assertEqual(row['expiry_date'], '2026-12-31')

    def test_items_data_without_tax_group_mirrors_with_vat_price(self):
        untaxed = Product.objects.create(
            internal_code='DET002', name='Untaxed Detail Product', delivery_price=Decimal('5.00'),
            sell_price=Decimal('8.00'), quantity=Decimal('1.000'),
        )
        DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=untaxed,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('5.00'),
        )
        response = self.client.get(reverse('delivery_details', args=[self.delivery.pk]))
        row = next(r for r in response.context['delivery_items_data'] if r['internal_code'] == 'DET002')
        self.assertEqual(row['price_without_vat'], 5.0)
        self.assertEqual(row['expiry_date'], '')


class WriteOffStockAdjustmentTests(TestCase):
    """movement_type=WRITE_OFF runs the exact same DeliveryItems.save()/
    post_delete machinery as a delivery, just with the sign flipped --
    these mirror DeliveryStockAdjustmentTests but assert stock going DOWN
    (and back up on delete), not up."""

    def setUp(self):
        self.user = User.objects.create_user(username='writeoff-receiver', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Write-off Supplier', bulstat='222222222')
        self.product = Product.objects.create(
            internal_code='WO0001', name='Spoiled Yogurt', delivery_price=Decimal('1.00'),
            sell_price=Decimal('2.00'), quantity=Decimal('20.000'),
        )
        self.write_off = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.user, supplier=self.supplier, document_date='2026-04-20',
        )

    def test_creating_item_decreases_stock(self):
        DeliveryItems.objects.create(
            delivery=self.write_off, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('15.000'))

    def test_editing_quantity_applies_only_the_delta(self):
        item = DeliveryItems.objects.create(
            delivery=self.write_off, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        item.delivery_quantity = Decimal('8.000')
        item.save()
        self.product.refresh_from_db()
        # 20 - 5 - (8-5) = 12, not 20 - 5 - 8.
        self.assertEqual(self.product.quantity, Decimal('12.000'))

    def test_deleting_item_restores_stock(self):
        item = DeliveryItems.objects.create(
            delivery=self.write_off, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('15.000'))
        item.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('20.000'))

    def test_negative_stock_is_allowed(self):
        # Business rule (CLAUDE.md): negative stock is intentional, a
        # signal to the manager, not something write-off validation blocks.
        DeliveryItems.objects.create(
            delivery=self.write_off, delivery_item=self.product,
            delivery_quantity=Decimal('50.000'), price_at_delivery=Decimal('1.00'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('-30.000'))


class WriteOffDocumentNumberTests(TestCase):
    """document_number is optional on a write-off -- if left blank,
    DeliveryAttributes.save() auto-generates an internal one so several
    same-day write-offs to the same supplier can still be told apart."""

    def setUp(self):
        self.user = User.objects.create_user(username='writeoff-receiver2', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Auto Number Supplier', bulstat='444444444')

    def test_blank_document_number_is_auto_generated(self):
        write_off = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.user, supplier=self.supplier, document_date='2026-04-20',
        )
        self.assertEqual(write_off.document_number, 'WO-20260420-001')

    def test_second_same_day_write_off_gets_next_sequence_number(self):
        DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.user, supplier=self.supplier, document_date='2026-04-20',
        )
        second = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.user, supplier=self.supplier, document_date='2026-04-20',
        )
        self.assertEqual(second.document_number, 'WO-20260420-002')

    def test_explicit_document_number_is_not_overwritten(self):
        write_off = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.user, supplier=self.supplier, document_date='2026-04-20',
            document_number='CUSTOM-1',
        )
        self.assertEqual(write_off.document_number, 'CUSTOM-1')

    def test_ordinary_delivery_document_number_stays_blank_if_not_given(self):
        # Auto-numbering is a write-off/scrap convenience only -- a real
        # delivery always has an actual supplier invoice/delivery-note
        # number, so nothing should be invented for it.
        invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, document_type=invoice_type,
            document_date='2026-04-20',
        )
        self.assertIsNone(delivery.document_number)


class WriteOffViewTests(TestCase):
    """End-to-end coverage for the write-off add screen -- same permission
    model as deliveries (Warehouse: add/change, Cashier: view-only), reuses
    DeliveryItemFormSet untouched, and redirects straight to the new
    record's details page (there's no write-off-specific list screen)."""

    def setUp(self):
        self.warehouse = User.objects.create_user(username='wo-warehouse', password='pass12345', role=User.WAREHOUSE)
        self.cashier = User.objects.create_user(username='wo-cashier', password='pass12345', role=User.CASHIER)
        self.supplier = Suppliers.objects.create(name='WO View Supplier', bulstat='888888888')
        self.product = Product.objects.create(
            internal_code='WOV001', name='Expired Milk', delivery_price=Decimal('1.20'),
            sell_price=Decimal('2.00'), quantity=Decimal('10.000'),
        )

    def test_write_off_add_page_loads_for_warehouse(self):
        self.client.force_login(self.warehouse)
        response = self.client.get(reverse('writeoff_add'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'deliveries/delivery_add.html')

    def test_cashier_cannot_add_write_off(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('writeoff_add'))
        self.assertEqual(response.status_code, 403)

    def test_creating_write_off_via_rebuilt_hidden_inputs(self):
        self.client.force_login(self.warehouse)
        data = {
            'supplier': self.supplier.pk,
            'time_of_delivery': '2026-04-20 10:00:00',
            'document_number': '',
            'document_date': '2026-04-20',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '3.000',
            'items-0-price_at_delivery': '1.20',
            'items-0-total_price_row': '3.60',
            'items-0-DELETE': '',
        }
        response = self.client.post(reverse('writeoff_add'), data)
        write_off = DeliveryAttributes.objects.get(movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF)
        self.assertRedirects(response, reverse('delivery_details', args=[write_off.pk]))

        self.assertEqual(write_off.document_number, 'WO-20260420-001')
        self.assertIsNone(write_off.document_type)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('7.000'))


class ScrapStockAdjustmentTests(TestCase):
    """movement_type=SCRAP decreases stock same as WRITE_OFF -- covered by
    DeliveryAttributes.OUTGOING_MOVEMENT_TYPES rather than a one-off check,
    these pin that down directly for SCRAP specifically."""

    def setUp(self):
        self.user = User.objects.create_user(username='scrap-receiver', password='pass12345')
        self.product = Product.objects.create(
            internal_code='SCR0001', name='Expired Cheese', delivery_price=Decimal('4.00'),
            sell_price=Decimal('6.00'), quantity=Decimal('10.000'),
        )
        self.scrap = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_SCRAP,
            receiver=self.user, document_date='2026-04-20',
        )

    def test_scrap_has_no_supplier(self):
        self.assertIsNone(self.scrap.supplier)

    def test_creating_item_decreases_stock(self):
        DeliveryItems.objects.create(
            delivery=self.scrap, delivery_item=self.product,
            delivery_quantity=Decimal('3.000'), price_at_delivery=Decimal('4.00'),
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('7.000'))

    def test_deleting_item_restores_stock(self):
        item = DeliveryItems.objects.create(
            delivery=self.scrap, delivery_item=self.product,
            delivery_quantity=Decimal('3.000'), price_at_delivery=Decimal('4.00'),
        )
        item.delete()
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('10.000'))

    def test_document_number_auto_generated_with_scrap_prefix(self):
        self.assertEqual(self.scrap.document_number, 'SCRAP-20260420-001')


class ScrapBatchTraceabilityTests(TestCase):
    """Scrap variant 1 (pick an existing delivered batch) stamps
    `source_item` back at the original DELIVERY row; variant 2 (free entry)
    leaves it blank -- both must coexist on the same scrap."""

    def setUp(self):
        self.user = User.objects.create_user(username='scrap-receiver2', password='pass12345')
        self.supplier = Suppliers.objects.create(name='Batch Supplier', bulstat='999999991')
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.product = Product.objects.create(
            internal_code='SCR0002', name='Yoghurt Batch', delivery_price=Decimal('2.00'),
            sell_price=Decimal('3.00'), quantity=Decimal('0.000'),
        )
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-BATCH-1', document_date='2026-04-01',
        )
        self.batch_item = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('10.000'), price_at_delivery=Decimal('2.00'),
            expiry_date='2026-05-01',
        )
        self.reason, _ = ScrapReason.objects.get_or_create(name='Expired')
        self.scrap = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_SCRAP,
            receiver=self.user, document_date='2026-05-02',
        )

    def test_variant_1_links_back_to_source_batch(self):
        scrap_item = DeliveryItems.objects.create(
            delivery=self.scrap, delivery_item=self.product,
            delivery_quantity=Decimal('4.000'), price_at_delivery=Decimal('2.00'),
            expiry_date=self.batch_item.expiry_date, source_item=self.batch_item,
            scrap_reason=self.reason,
        )
        self.assertEqual(scrap_item.source_item_id, self.batch_item.pk)
        self.assertIn(scrap_item, self.batch_item.scrapped_as.all())

    def test_variant_2_free_entry_has_no_source_item(self):
        scrap_item = DeliveryItems.objects.create(
            delivery=self.scrap, delivery_item=self.product,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('2.00'),
            scrap_reason=self.reason,
        )
        self.assertIsNone(scrap_item.source_item)

    def test_deleting_source_delivery_item_does_not_delete_scrap_history(self):
        scrap_item = DeliveryItems.objects.create(
            delivery=self.scrap, delivery_item=self.product,
            delivery_quantity=Decimal('4.000'), price_at_delivery=Decimal('2.00'),
            source_item=self.batch_item,
        )
        self.batch_item.delete()
        scrap_item.refresh_from_db()
        self.assertIsNone(scrap_item.source_item)


class ScrapReasonCRUDTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='scrap-manager', password='pass12345', role=User.MANAGER)
        self.warehouse = User.objects.create_user(username='scrap-warehouse', password='pass12345', role=User.WAREHOUSE)

    def test_manager_can_list_and_create(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('scrap_reason_list')).status_code, 200)
        response = self.client.post(reverse('scrap_reason_create'), {'name': 'Damaged'})
        self.assertTrue(ScrapReason.objects.filter(name='Damaged').exists())
        self.assertRedirects(response, reverse('scrap_reason_list'))

    def test_warehouse_can_view_but_not_create(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('scrap_reason_list')).status_code, 200)
        response = self.client.post(reverse('scrap_reason_create'), {'name': 'Damaged'})
        self.assertEqual(response.status_code, 403)


class BatchSearchViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='batch-search-user', password='pass12345')
        self.client.force_login(self.user)
        self.supplier = Suppliers.objects.create(name='Batch Search Supplier', bulstat='999999992')
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.product = Product.objects.create(
            internal_code='BS0001', name='Yogurt', delivery_price=Decimal('1.00'),
            sell_price=Decimal('2.00'), quantity=Decimal('5.000'),
        )
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-BS-1', document_date='2026-04-01',
        )
        self.with_expiry = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('5.000'), price_at_delivery=Decimal('1.00'),
            expiry_date='2026-05-01',
        )
        self.without_expiry = DeliveryItems.objects.create(
            delivery=self.delivery, delivery_item=self.product,
            delivery_quantity=Decimal('2.000'), price_at_delivery=Decimal('1.00'),
        )

    def test_only_items_with_expiry_date_are_returned(self):
        response = self.client.get(reverse('batch_search'))
        ids = [r['id'] for r in response.json()['results']]
        self.assertIn(self.with_expiry.pk, ids)
        self.assertNotIn(self.without_expiry.pk, ids)

    def test_write_off_and_scrap_batches_are_excluded(self):
        write_off = DeliveryAttributes.objects.create(
            movement_type=DeliveryAttributes.MOVEMENT_WRITE_OFF,
            receiver=self.user, supplier=self.supplier, document_date='2026-04-20',
        )
        excluded = DeliveryItems.objects.create(
            delivery=write_off, delivery_item=self.product,
            delivery_quantity=Decimal('1.000'), price_at_delivery=Decimal('1.00'),
            expiry_date='2026-06-01',
        )
        response = self.client.get(reverse('batch_search'))
        ids = [r['id'] for r in response.json()['results']]
        self.assertNotIn(excluded.pk, ids)


class ScrapViewTests(TestCase):
    """End-to-end coverage for the scrap add screen, both entry variants."""

    def setUp(self):
        self.warehouse = User.objects.create_user(username='scrap-view-warehouse', password='pass12345', role=User.WAREHOUSE)
        self.cashier = User.objects.create_user(username='scrap-view-cashier', password='pass12345', role=User.CASHIER)
        self.supplier = Suppliers.objects.create(name='Scrap View Supplier', bulstat='999999993')
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.product = Product.objects.create(
            internal_code='SCV001', name='Old Yogurt', delivery_price=Decimal('1.50'),
            sell_price=Decimal('2.50'), quantity=Decimal('10.000'),
        )
        self.reason, _ = ScrapReason.objects.get_or_create(name='Expired')

    def test_scrap_add_page_loads_for_warehouse(self):
        self.client.force_login(self.warehouse)
        response = self.client.get(reverse('scrap_add'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'deliveries/delivery_add.html')

    def test_cashier_cannot_add_scrap(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('scrap_add'))
        self.assertEqual(response.status_code, 403)

    def test_creating_scrap_with_batch_reference_and_reason(self):
        self.client.force_login(self.warehouse)
        delivery = DeliveryAttributes.objects.create(
            receiver=self.warehouse, supplier=self.supplier, document_type=self.invoice_type,
            document_number='INV-SCV-1', document_date='2026-04-01',
        )
        batch_item = DeliveryItems.objects.create(
            delivery=delivery, delivery_item=self.product,
            delivery_quantity=Decimal('10.000'), price_at_delivery=Decimal('1.50'),
            expiry_date='2026-05-01',
        )
        data = {
            'receiver': self.warehouse.pk,
            'time_of_delivery': '2026-05-02 10:00:00',
            'document_number': '',
            'document_date': '2026-05-02',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '4.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '6.00',
            'items-0-source_item': batch_item.pk,
            'items-0-scrap_reason': self.reason.pk,
            'items-0-DELETE': '',
        }
        response = self.client.post(reverse('scrap_add'), data)
        scrap = DeliveryAttributes.objects.get(movement_type=DeliveryAttributes.MOVEMENT_SCRAP)
        self.assertRedirects(response, reverse('delivery_details', args=[scrap.pk]))

        scrap_item = scrap.items.get()
        self.assertEqual(scrap_item.source_item_id, batch_item.pk)
        self.assertEqual(scrap_item.scrap_reason_id, self.reason.pk)
        self.assertIsNone(scrap.supplier)
        self.assertEqual(scrap.document_number, 'SCRAP-20260502-001')
        self.product.refresh_from_db()
        # setUp starts the product at 10.000; the batch delivery created in
        # this test adds another +10.000, then scrapping 4.000 subtracts it.
        self.assertEqual(self.product.quantity, Decimal('16.000'))

    def test_creating_scrap_free_entry_without_batch_reference(self):
        self.client.force_login(self.warehouse)
        data = {
            'receiver': self.warehouse.pk,
            'time_of_delivery': '2026-05-02 10:00:00',
            'document_number': '',
            'document_date': '2026-05-02',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': self.product.pk,
            'items-0-delivery_quantity': '2.000',
            'items-0-price_at_delivery': '1.50',
            'items-0-total_price_row': '3.00',
            'items-0-source_item': '',
            'items-0-scrap_reason': self.reason.pk,
            'items-0-DELETE': '',
        }
        response = self.client.post(reverse('scrap_add'), data)
        scrap = DeliveryAttributes.objects.get(movement_type=DeliveryAttributes.MOVEMENT_SCRAP)
        self.assertRedirects(response, reverse('delivery_details', args=[scrap.pk]))

        scrap_item = scrap.items.get()
        self.assertIsNone(scrap_item.source_item)
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('8.000'))


def _build_import_xlsx(rows):
    """rows: list of tuples matching excel_import.COLUMN_HEADERS order."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Code', 'Name', 'Unit Type', 'Quantity', 'Category', 'Subcategory', 'Delivery Price',
               'Sell Price', 'Main Supplier', 'Barcode 1', 'Barcode 2', 'Barcode 3'])
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class ExcelImportParsingTests(TestCase):
    """Unit tests for excel_import.parse_delivery_import -- pure parsing/
    matching, no view/formset/database-write involved."""

    def setUp(self):
        self.product = Product.objects.create(
            internal_code='EXIST01', name='Existing Import Product',
            delivery_price=Decimal('1.00'), sell_price=Decimal('2.00'), quantity=Decimal('5.000'),
        )
        Barcode.objects.create(product=self.product, code='9990001112223', position=1)
        self.category = Category.objects.create(name='Import Top Category')
        self.subcategory = Category.objects.create(name='Import Sub Category', parent=self.category)
        self.supplier = Suppliers.objects.create(name='Import Test Supplier', bulstat='444555666')

    def test_code_match_with_positive_quantity_delivers(self):
        buf = _build_import_xlsx([('EXIST01', self.product.name, '', 3, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['rows']), 1)
        row = result['rows'][0]
        self.assertFalse(row['is_new'])
        self.assertEqual(row['product_id'], self.product.pk)
        self.assertEqual(row['quantity'], 3.0)

    def test_barcode_match_with_zero_quantity_is_skipped(self):
        buf = _build_import_xlsx([('', 'Whatever Name', '', 0, '', '', '', '', '', '9990001112223', '', '')])
        result = parse_delivery_import(buf)
        self.assertEqual(result['rows'], [])
        self.assertEqual(len(result['skipped']), 1)
        self.assertIn(self.product.name, result['skipped'][0])

    def test_new_product_resolves_category_subcategory_and_supplier(self):
        buf = _build_import_xlsx([(
            '', 'Brand New Import Item', '', 4, self.category.name, self.subcategory.name,
            '1.20', '2.40', self.supplier.name, '', '', '',
        )])
        result = parse_delivery_import(buf)
        self.assertEqual(result['errors'], [])
        row = result['rows'][0]
        self.assertTrue(row['is_new'])
        self.assertEqual(row['category_id'], self.subcategory.pk)
        self.assertEqual(row['supplier_id'], self.supplier.pk)
        self.assertEqual(row['delivery_price'], 1.2)
        self.assertEqual(row['sell_price'], 2.4)
        self.assertTrue(row['internal_code'])

    def test_new_product_flags_fuzzy_name_duplicate(self):
        # Reordered words -- same trigram-similarity idea already used
        # elsewhere in the app (category/supplier search).
        buf = _build_import_xlsx([('', 'Import Product Existing', '', 1, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertTrue(row['is_new'])
        self.assertIsNotNone(row['duplicate_candidate'])
        self.assertEqual(row['duplicate_candidate']['id'], self.product.pk)

    def test_unmatched_category_and_supplier_are_left_blank(self):
        buf = _build_import_xlsx([(
            '', 'Totally Unrelated New Product Zzz', '', 1, 'Nonexistent Category Xyz', '',
            '', '', 'Nonexistent Supplier Xyz', '', '', '',
        )])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertIsNone(row['category_id'])
        self.assertIsNone(row['supplier_id'])

    def test_missing_name_is_an_error_not_a_row(self):
        buf = _build_import_xlsx([('', '', '', 1, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        self.assertEqual(result['rows'], [])
        self.assertEqual(len(result['errors']), 1)

    def test_blank_rows_are_silently_ignored(self):
        buf = _build_import_xlsx([tuple([None] * 12)])
        result = parse_delivery_import(buf)
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['errors'], [])

    def test_new_product_unit_type_kg_is_recognised(self):
        # Bulgarian spelling accepted too, case-insensitive.
        buf = _build_import_xlsx([('', 'Weight Sold Import Item', 'КГ', 2, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertEqual(row['unit_type'], Product.WEIGHT)

    def test_new_product_unit_type_defaults_to_piece_when_blank(self):
        buf = _build_import_xlsx([('', 'Piece Sold Import Item', '', 2, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertEqual(row['unit_type'], Product.PIECE)

    # Layer 2 (AI-assisted matching for the ambiguous middle band) tests.
    # 'Imported Existing Goods' vs self.product's name ('Existing Import
    # Product') sits at ~0.455 trigram similarity -- confirmed via a direct
    # `SELECT similarity(...)` against this same Postgres instance, squarely
    # between SIMILARITY_LOW (0.30) and SIMILARITY_HIGH (0.55): too similar
    # to ignore outright, not similar enough for Layer 1 alone to accept.

    @override_settings(ANTHROPIC_API_KEY='')
    def test_ambiguous_match_without_ai_key_defaults_to_no_match(self):
        buf = _build_import_xlsx([('', 'Imported Existing Goods', '', 1, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        # Layer 1 on its own is conservative -- an ambiguous pair with no
        # Layer 2 available to weigh in resolves to "not a match", same as
        # if similarity had been below SIMILARITY_LOW entirely.
        self.assertIsNone(row['duplicate_candidate'])

    @override_settings(ANTHROPIC_API_KEY='fake-key-for-test')
    @patch('STORA.deliveries.excel_import._ai_judge_ambiguous_matches')
    def test_ambiguous_match_with_ai_key_accepts_when_ai_confirms(self, mock_judge):
        mock_judge.return_value = {(2, '_duplicate'): True}
        buf = _build_import_xlsx([('', 'Imported Existing Goods', '', 1, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertIsNotNone(row['duplicate_candidate'])
        self.assertEqual(row['duplicate_candidate']['id'], self.product.pk)

    @override_settings(ANTHROPIC_API_KEY='fake-key-for-test')
    @patch('STORA.deliveries.excel_import._ai_judge_ambiguous_matches')
    def test_ambiguous_match_with_ai_key_rejects_when_ai_denies(self, mock_judge):
        mock_judge.return_value = {(2, '_duplicate'): False}
        buf = _build_import_xlsx([('', 'Imported Existing Goods', '', 1, '', '', '', '', '', '', '', '')])
        result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertIsNone(row['duplicate_candidate'])

    @override_settings(ANTHROPIC_API_KEY='fake-key-for-test')
    def test_ai_judge_batches_every_ambiguous_pair_into_one_call(self):
        # Two separate rows, each with one ambiguous field (category on the
        # first, supplier on the second) -- both must be judged, but in a
        # single API round trip, not one call per row/field.
        from STORA.deliveries.excel_import import _ai_judge_ambiguous_matches

        text_block = SimpleNamespace(type='text', text='[true, false]')
        mock_response = SimpleNamespace(content=[text_block])
        with patch('STORA.deliveries.excel_import.anthropic.Anthropic') as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.messages.create.return_value = mock_response

            buf = _build_import_xlsx([
                ('', 'New Item One', '', 1, 'Category of Imported Goods', '', '', '', '', '', '', ''),
                ('', 'New Item Two', '', 1, '', '', '', '', 'Imports Testing Supply', '', '', ''),
            ])
            result = parse_delivery_import(buf)

        mock_client.messages.create.assert_called_once()
        prompt = mock_client.messages.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('Category of Imported Goods', prompt)
        self.assertIn('Imports Testing Supply', prompt)

        row1, row2 = result['rows']
        # First pending item (category, index 0 in the batch) -> True.
        self.assertEqual(row1['category_id'], self.category.pk)
        # Second pending item (supplier, index 1) -> False.
        self.assertIsNone(row2['supplier_id'])

    @override_settings(ANTHROPIC_API_KEY='fake-key-for-test')
    def test_ai_judge_failure_degrades_to_no_match_without_crashing(self):
        with patch('STORA.deliveries.excel_import.anthropic.Anthropic') as mock_anthropic_cls:
            mock_anthropic_cls.return_value.messages.create.side_effect = RuntimeError('network boom')
            buf = _build_import_xlsx([('', 'Imported Existing Goods', '', 1, '', '', '', '', '', '', '', '')])
            result = parse_delivery_import(buf)
        row = result['rows'][0]
        self.assertIsNone(row['duplicate_candidate'])


class ExcelImportViewTests(TestCase):
    def setUp(self):
        self.warehouse = User.objects.create_user(username='wh_import', password='pass12345', role=User.WAREHOUSE)
        self.cashier = User.objects.create_user(username='cash_import', password='pass12345', role=User.CASHIER)
        self.product = Product.objects.create(
            internal_code='VIEWIMP1', name='View Import Product',
            delivery_price=Decimal('1.00'), sell_price=Decimal('2.00'), quantity=Decimal('0.000'),
        )

    def test_template_download_requires_permission(self):
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('delivery_import_template'))
        self.assertEqual(response.status_code, 403)

    def test_template_download_returns_correct_headers(self):
        self.client.force_login(self.warehouse)
        response = self.client.get(reverse('delivery_import_template'))
        self.assertEqual(response.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        headers = [c.value for c in wb.active[1]]
        self.assertEqual(headers, ['Code', 'Name', 'Unit Type', 'Quantity', 'Category', 'Subcategory',
                                    'Delivery Price', 'Sell Price', 'Main Supplier',
                                    'Barcode 1', 'Barcode 2', 'Barcode 3'])

    def test_import_endpoint_requires_permission(self):
        self.client.force_login(self.cashier)
        buf = _build_import_xlsx([('VIEWIMP1', self.product.name, '', 2, '', '', '', '', '', '', '', '')])
        response = self.client.post(
            reverse('delivery_import_excel'),
            {'file': SimpleUploadedFile('import.xlsx', buf.read())},
        )
        self.assertEqual(response.status_code, 403)

    def test_import_endpoint_returns_parsed_rows(self):
        self.client.force_login(self.warehouse)
        buf = _build_import_xlsx([('VIEWIMP1', self.product.name, '', 2, '', '', '', '', '', '', '', '')])
        response = self.client.post(
            reverse('delivery_import_excel'),
            {'file': SimpleUploadedFile('import.xlsx', buf.read())},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['rows'][0]['product_id'], self.product.pk)

    def test_import_endpoint_without_file_returns_400(self):
        self.client.force_login(self.warehouse)
        response = self.client.post(reverse('delivery_import_excel'), {})
        self.assertEqual(response.status_code, 400)


class DeliveryAddWithNewProductRowsTests(TestCase):
    """End-to-end POST tests for deliveries_add's handling of Excel-import
    "pending new product" rows -- posts the exact hidden-input shape
    rebuildHiddenInputs() produces for a buildNewProductRow (see
    _delivery_items_table.html), standing in for the JS the same way
    DeliveryFormSubmissionTests does for normal rows."""

    def setUp(self):
        self.warehouse = User.objects.create_user(username='wh_newprod', password='pass12345', role=User.WAREHOUSE)
        self.supplier = Suppliers.objects.create(name='New Product Row Supplier', bulstat='777888999')
        self.category = Category.objects.create(name='New Product Row Category')
        self.invoice_type, _ = DocumentType.objects.get_or_create(name='Invoice')
        self.client.force_login(self.warehouse)

    def _general_info(self, **overrides):
        data = {
            'supplier': self.supplier.pk,
            'time_of_delivery': '2026-04-20 10:00:00',
            'document_type': self.invoice_type.pk,
            'document_number': 'IMP-100',
            'document_date': '2026-04-20',
        }
        data.update(overrides)
        return data

    def test_positive_quantity_creates_product_and_delivers_it(self):
        data = self._general_info(document_number='IMP-101')
        data.update({
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': '',
            'items-0-delivery_quantity': '6.000',
            'items-0-price_at_delivery': '3.00',
            'items-0-total_price_row': '18.00',
            'items-0-DELETE': '',
            'items-0-new_product': '1',
            'items-0-new_product_name': 'Imported New Product One',
            'items-0-new_product_code': '',
            'items-0-new_product_unit_type': 'weight',
            'items-0-new_product_delivery_price': '3.00',
            'items-0-new_product_sell_price': '5.00',
            'items-0-new_product_category': str(self.category.pk),
            'items-0-new_product_supplier': str(self.supplier.pk),
            'items-0-new_product_barcode_1': '1231231231234',
            'items-0-new_product_barcode_2': '',
            'items-0-new_product_barcode_3': '',
        })
        response = self.client.post(reverse('delivery_add'), data)
        self.assertRedirects(response, reverse('delivery_add'))

        product = Product.objects.get(name='Imported New Product One')
        self.assertEqual(product.quantity, Decimal('6.000'))
        self.assertEqual(product.category_id, self.category.pk)
        self.assertEqual(product.sell_price, Decimal('5.00'))
        self.assertEqual(product.unit_type, Product.WEIGHT)
        self.assertTrue(Barcode.objects.filter(product=product, code='1231231231234').exists())
        self.assertTrue(ProductSupplier.objects.filter(product=product, supplier=self.supplier).exists())

        delivery = DeliveryAttributes.objects.get(document_number='IMP-101')
        self.assertEqual(delivery.items.get().delivery_item, product)

    def test_zero_quantity_creates_product_only_no_delivery_item(self):
        # Paired with a normal qty>0 row -- a delivery consisting of ONLY a
        # qty=0 "just catalog it" row has nothing left at all for the
        # items formset once that row is stripped (see
        # _materialize_pending_products), and the pre-existing "must add
        # at least one delivery item" formset rule (unrelated to this
        # feature, not something to special-case around) correctly refuses
        # to save a delivery with zero real items -- exactly what a real
        # Excel batch that's ENTIRELY zero-quantity rows would also hit.
        # This test covers the realistic mixed case instead.
        other_product = Product.objects.create(
            internal_code='IMPCOMP1', name='Companion Delivered Product',
            delivery_price=Decimal('1.00'), sell_price=Decimal('2.00'), quantity=Decimal('0.000'),
        )
        data = self._general_info(document_number='IMP-102')
        data.update({
            'items-TOTAL_FORMS': '2',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': other_product.pk,
            'items-0-delivery_quantity': '1.000',
            'items-0-price_at_delivery': '1.00',
            'items-0-total_price_row': '1.00',
            'items-0-DELETE': '',
            'items-1-id': '',
            'items-1-delivery_item': '',
            'items-1-delivery_quantity': '0',
            'items-1-price_at_delivery': '',
            'items-1-total_price_row': '',
            'items-1-DELETE': '',
            'items-1-new_product': '1',
            'items-1-new_product_name': 'Imported New Product Zero Qty',
            'items-1-new_product_code': '',
            'items-1-new_product_delivery_price': '',
            'items-1-new_product_sell_price': '',
            'items-1-new_product_category': '',
            'items-1-new_product_supplier': '',
            'items-1-new_product_barcode_1': '',
            'items-1-new_product_barcode_2': '',
            'items-1-new_product_barcode_3': '',
        })
        response = self.client.post(reverse('delivery_add'), data)
        self.assertRedirects(response, reverse('delivery_add'))

        product = Product.objects.get(name='Imported New Product Zero Qty')
        self.assertEqual(product.quantity, Decimal('0.000'))
        # No new_product_unit_type field posted at all -- falls back to the
        # default (Product.PIECE), same as Product's own model default.
        self.assertEqual(product.unit_type, Product.PIECE)
        # sell_price required by the model (min 0.01) -- falls back to a
        # placeholder since neither delivery nor sell price was given.
        self.assertEqual(product.sell_price, Decimal('0.01'))
        self.assertFalse(DeliveryItems.objects.filter(delivery_item=product).exists())

        other_product.refresh_from_db()
        self.assertEqual(other_product.quantity, Decimal('1.000'))

    def test_validation_failure_rolls_back_pending_product_creation(self):
        # Missing document_date -- form.is_valid() fails, the whole atomic
        # block (including the product this row would have created) must
        # roll back, not leave an orphan product behind.
        data = self._general_info(document_number='IMP-103', document_date='')
        data.update({
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '',
            'items-0-delivery_item': '',
            'items-0-delivery_quantity': '2.000',
            'items-0-price_at_delivery': '1.00',
            'items-0-total_price_row': '2.00',
            'items-0-DELETE': '',
            'items-0-new_product': '1',
            'items-0-new_product_name': 'Should Not Be Created',
            'items-0-new_product_code': '',
            'items-0-new_product_delivery_price': '1.00',
            'items-0-new_product_sell_price': '',
            'items-0-new_product_category': '',
            'items-0-new_product_supplier': '',
            'items-0-new_product_barcode_1': '',
            'items-0-new_product_barcode_2': '',
            'items-0-new_product_barcode_3': '',
        })
        response = self.client.post(reverse('delivery_add'), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Should Not Be Created').exists())