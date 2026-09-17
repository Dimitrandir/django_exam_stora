from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType, ScrapReason, Suppliers
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