from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType, Suppliers
from STORA.products.models import Product, TaxGroup


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
        self.assertRedirects(response, reverse('deliveries_list'))

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
        self.assertRedirects(response, reverse('deliveries_list'))

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