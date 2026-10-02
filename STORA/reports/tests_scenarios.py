"""QA end-to-end scenarios -- "a day (and a half) in the shop".

Unlike the per-app unit tests, these drive the REAL views (POSTs through
the same forms/formsets the browser submits) across several apps in a row,
then check that stock levels and every report agree with what physically
happened. Each assertion message says in plain words what a wrong number
would mean for the shop.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from STORA.accounts.models import Employee
from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType, ScrapReason
from STORA.products.models import Category, Product, ProductSupplier, RecipeIngredient, Suppliers, TaxGroup
from STORA.reports.services import stock_as_of
from STORA.revisions.models import RevisionAttributes, RevisionItems
from STORA.sales.models import RefundAttributes, SaleAttributes, SaleItems


D = Decimal


def delivery_post(supplier, doc_type, doc_number, doc_date, rows, scrap=False):
    """rows: list of dicts with product/qty/price (+ optional id/DELETE/expiry/scrap_reason)."""
    data = {
        'document_date': doc_date.isoformat(),
        'time_of_delivery': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
        'comment': '',
        'items-TOTAL_FORMS': str(len(rows)),
        'items-INITIAL_FORMS': str(sum(1 for r in rows if r.get('id'))),
        'items-MIN_NUM_FORMS': '0',
        'items-MAX_NUM_FORMS': '1000',
    }
    if supplier is not None:
        data['supplier'] = str(supplier.pk)
    if doc_type is not None:
        data['document_type'] = str(doc_type.pk)
    if doc_number is not None:
        data['document_number'] = doc_number
    for i, row in enumerate(rows):
        p = f'items-{i}-'
        data[p + 'delivery_item'] = str(row['product'].pk)
        data[p + 'delivery_quantity'] = str(row['qty'])
        data[p + 'price_at_delivery'] = str(row.get('price', ''))
        data[p + 'total_price_row'] = ''
        data[p + 'expiry_date'] = row.get('expiry', '')
        data[p + 'source_item'] = ''
        data[p + 'scrap_reason'] = str(row['scrap_reason'].pk) if row.get('scrap_reason') else ''
        if row.get('id'):
            data[p + 'id'] = str(row['id'])
        if row.get('DELETE'):
            data[p + 'DELETE'] = 'on'
    return data


def sale_post(rows, method='CASH', paid=None, card=None, change=None):
    total = sum(D(str(r['qty'])) * D(str(r['price'])) for r in rows).quantize(D('0.01'))
    if method == 'CASH':
        paid = paid if paid is not None else total
        card = D('0')
        change = D(str(paid)) - total
    elif method == 'CARD':
        paid, card, change = D('0'), total, D('0')
    data = {
        'payment_method': method,
        'amount_paid': str(paid),
        'card_amount': str(card),
        'change_due': str(change),
        'items-TOTAL_FORMS': str(len(rows)),
        'items-INITIAL_FORMS': '0',
        'items-MIN_NUM_FORMS': '0',
        'items-MAX_NUM_FORMS': '1000',
    }
    for i, row in enumerate(rows):
        p = f'items-{i}-'
        data[p + 'sale_item'] = str(row['product'].pk)
        data[p + 'sale_quantity'] = str(row['qty'])
        data[p + 'price_at_sale'] = str(row['price'])
        data[p + 'total_price_row'] = ''
    return data


class ShopDayScenarioTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = Employee.objects.create_user('qa_manager', password='pw', role=Employee.MANAGER,
                                                   first_name='Maria', last_name='Manager')
        cls.warehouse = Employee.objects.create_user('qa_warehouse', password='pw', role=Employee.WAREHOUSE,
                                                     first_name='Wes', last_name='Warehouse')
        cls.cashier = Employee.objects.create_user('qa_cashier', password='pw', role=Employee.CASHIER,
                                                   first_name='Cara', last_name='Cashier')

        cls.vat20 = TaxGroup.objects.create(name='VAT 20%', rate=D('20'))
        cls.vat9 = TaxGroup.objects.create(name='VAT 9%', rate=D('9'))
        cls.drinks = Category.objects.create(name='Drinks')
        cls.dairy = Category.objects.create(name='Dairy')
        cls.cafe = Category.objects.create(name='Cafe')
        cls.supplier = Suppliers.objects.create(name='Coca-Cola HBC', bulstat='123456789')
        cls.supplier2 = Suppliers.objects.create(name='Dairy Farm', bulstat='987654321')
        cls.invoice, _ = DocumentType.objects.get_or_create(name='Invoice')
        cls.expired, _ = ScrapReason.objects.get_or_create(name='Expired')

        cls.cola = Product.objects.create(internal_code='1001', name='Coca-Cola 0.5L', unit_type='piece',
                                          delivery_price=D('1.20'), sell_price=D('2.00'),
                                          category=cls.drinks, tax_group=cls.vat20)
        cls.cheese = Product.objects.create(internal_code='1002', name='Cheese (kg)', unit_type='weight',
                                            delivery_price=D('10.90'), sell_price=D('16.00'),
                                            category=cls.dairy, tax_group=cls.vat9)
        cls.milk = Product.objects.create(internal_code='1003', name='Milk (kg)', unit_type='weight',
                                          delivery_price=D('1.80'), sell_price=D('2.60'),
                                          category=cls.dairy, tax_group=cls.vat20)
        cls.coffee = Product.objects.create(internal_code='1004', name='Coffee beans (kg)', unit_type='weight',
                                            delivery_price=D('30.00'), sell_price=D('45.00'),
                                            category=cls.cafe, tax_group=cls.vat20)
        cls.cappuccino = Product.objects.create(internal_code='1005', name='Cappuccino', unit_type='piece',
                                                sell_price=D('3.50'), category=cls.cafe, tax_group=cls.vat20,
                                                is_recipe=True)
        RecipeIngredient.objects.create(recipe=cls.cappuccino, ingredient=cls.milk, quantity=D('0.150'))
        RecipeIngredient.objects.create(recipe=cls.cappuccino, ingredient=cls.coffee, quantity=D('0.010'))

    def qty(self, product):
        return Product.objects.get(pk=product.pk).quantity

    def login(self, user):
        self.client.force_login(user)

    # ------------------------------------------------------------------
    # Building blocks used by the scenarios
    # ------------------------------------------------------------------
    def receive_morning_delivery(self, doc_date):
        self.login(self.warehouse)
        response = self.client.post(reverse('delivery_add'), delivery_post(
            self.supplier, self.invoice, 'INV-0001', doc_date, [
                {'product': self.cola, 'qty': 48, 'price': '1.20', 'expiry': (timezone.localdate() + timedelta(days=10)).isoformat()},
                {'product': self.cheese, 'qty': '5.250', 'price': '10.90'},
                {'product': self.milk, 'qty': '10.000', 'price': '1.80'},
                {'product': self.coffee, 'qty': '2.000', 'price': '30.00'},
            ],
        ))
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['formset'].errors)
        return DeliveryAttributes.objects.latest('pk')

    def ring_up(self, rows, **kwargs):
        self.login(self.cashier)
        response = self.client.post(reverse('sale_add'), sale_post(rows, **kwargs))
        self.assertEqual(response.status_code, 302, (response.context["form"].errors, response.context["formset"].errors, response.context["formset"].non_form_errors()) if response.status_code == 200 else "")
        return SaleAttributes.objects.latest('pk')

    # ------------------------------------------------------------------
    # 1. Delivery -> stock, totals, supplier link, delivery report
    # ------------------------------------------------------------------
    def test_delivery_updates_stock_totals_supplier_link_and_report(self):
        delivery = self.receive_morning_delivery(timezone.localdate())

        self.assertEqual(self.qty(self.cola), D('48'))
        self.assertEqual(self.qty(self.cheese), D('5.250'))
        # 48*1.20 + 5.25*10.90 + 10*1.80 + 2*30 = 57.60 + 57.225 + 18 + 60
        self.assertEqual(delivery.total_amount, D('192.83'),
                         'delivery total should be the sum of its rows (57.225 rounds per row to 57.23)')
        self.assertTrue(ProductSupplier.objects.filter(product=self.cola, supplier=self.supplier, position=1).exists(),
                        'first delivery from a supplier should make it the product primary supplier')

        self.login(self.manager)
        response = self.client.get(reverse('deliveries_report'))
        rows = response.context['deliveries_data']
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['total_amount'], 192.83, places=2)
        # Without VAT: cola/milk/coffee at 20%, cheese at 9%
        expected_net = (D('57.60') + D('18') + D('60')) / D('1.2') + D('5.25') * (D('10.90') / D('1.09'))
        self.assertAlmostEqual(rows[0]['total_amount_without_vat'], float(expected_net), places=1)

    def test_editing_delivery_quantity_applies_only_the_difference(self):
        delivery = self.receive_morning_delivery(timezone.localdate())
        cola_row = delivery.items.get(delivery_item=self.cola)
        other_rows = list(delivery.items.exclude(pk=cola_row.pk))

        self.login(self.warehouse)
        data = delivery_post(self.supplier, self.invoice, 'INV-0001', timezone.localdate(), [
            {'id': cola_row.pk, 'product': self.cola, 'qty': 50, 'price': '1.20'},
        ] + [{'id': r.pk, 'product': r.delivery_item, 'qty': r.delivery_quantity, 'price': r.price_at_delivery}
             for r in other_rows])
        data['receiver'] = str(self.warehouse.pk)
        response = self.client.post(reverse('delivery_edit', args=[delivery.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.qty(self.cola), D('50'), 'editing 48 -> 50 must add only +2, not 50 again')
        self.assertEqual(self.qty(self.cheese), D('5.250'), 'untouched rows must not change stock')

    def test_swapping_product_on_existing_delivery_row_moves_stock_correctly(self):
        """A POSTed edit that changes WHICH product an existing row points
        to (instead of deleting+re-adding the row) must take the old
        product's stock back and give the new one the full quantity."""
        delivery = self.receive_morning_delivery(timezone.localdate())
        milk_row = delivery.items.get(delivery_item=self.milk)
        rows = [{'id': r.pk, 'product': r.delivery_item, 'qty': r.delivery_quantity, 'price': r.price_at_delivery}
                for r in delivery.items.exclude(pk=milk_row.pk)]
        rows.append({'id': milk_row.pk, 'product': self.cheese, 'qty': '10.000', 'price': '1.80'})

        self.login(self.warehouse)
        data = delivery_post(self.supplier, self.invoice, 'INV-0001', timezone.localdate(), rows)
        data['receiver'] = str(self.warehouse.pk)
        self.client.post(reverse('delivery_edit', args=[delivery.pk]), data)

        self.assertEqual(self.qty(self.milk), D('0'), 'milk was swapped out of the delivery -- its 10 kg must go back out')
        self.assertEqual(self.qty(self.cheese), D('15.250'), 'cheese should now have its 5.25 + the swapped-in 10')

    def test_delivery_at_new_price_updates_catalog_delivery_price(self):
        from STORA.products.models import ProductChangeLog
        self.login(self.warehouse)
        today = timezone.localdate()
        self.client.post(reverse('delivery_add'), delivery_post(
            self.supplier2, self.invoice, 'INV-A', today, [{'product': self.milk, 'qty': '5', 'price': '1.95'}]))
        self.assertEqual(Product.objects.get(pk=self.milk.pk).delivery_price, D('1.95'))
        log = ProductChangeLog.objects.filter(product=self.milk, field_name='delivery_price').latest('pk')
        self.assertEqual((log.old_value, log.new_value, log.changed_by), ('1.80', '1.95', self.warehouse))

        # A newer delivery at 2.10, then a correction to the OLDER one --
        # the catalog must keep the latest price, not roll back.
        older = DeliveryAttributes.objects.latest('pk')
        DeliveryAttributes.objects.filter(pk=older.pk).update(time_of_delivery=timezone.now() - timedelta(hours=2))
        self.client.post(reverse('delivery_add'), delivery_post(
            self.supplier2, self.invoice, 'INV-B', today, [{'product': self.milk, 'qty': '5', 'price': '2.10'}]))
        row = older.items.get()
        data = delivery_post(self.supplier2, self.invoice, 'INV-A', today,
                             [{'id': row.pk, 'product': self.milk, 'qty': '5', 'price': '1.90'}])
        data['receiver'] = str(self.warehouse.pk)
        older.refresh_from_db()
        data['time_of_delivery'] = timezone.localtime(older.time_of_delivery).strftime('%Y-%m-%d %H:%M:%S')
        self.client.post(reverse('delivery_edit', args=[older.pk]), data)
        self.assertEqual(Product.objects.get(pk=self.milk.pk).delivery_price, D('2.10'))

        # Write-offs never touch the catalog price.
        self.client.post(reverse('writeoff_add'), delivery_post(
            self.supplier2, None, '', today, [{'product': self.milk, 'qty': '1', 'price': '0.50'}]))
        self.assertEqual(Product.objects.get(pk=self.milk.pk).delivery_price, D('2.10'))

    # ------------------------------------------------------------------
    # 2. Sales: piece, weight, recipe, payment methods, report numbers
    # ------------------------------------------------------------------
    def test_sales_day_stock_and_reports(self):
        self.receive_morning_delivery(timezone.localdate())

        s1 = self.ring_up([{'product': self.cola, 'qty': 2, 'price': '2.00'},
                           {'product': self.cheese, 'qty': '0.350', 'price': '16.00'}], method='CASH', paid=D('20'))
        s2 = self.ring_up([{'product': self.cappuccino, 'qty': 3, 'price': '3.50'}], method='CARD')
        s3 = self.ring_up([{'product': self.cola, 'qty': 1, 'price': '2.00'}], method='MIXED',
                          paid=D('0.50'), card=D('1.50'), change=D('0'))

        self.assertEqual(s1.total_amount, D('9.60'))  # 4.00 + 5.60
        self.assertEqual(s1.change_due, D('10.40'))
        self.assertEqual(s2.total_amount, D('10.50'))
        for sale in (s1, s2, s3):
            sale.refresh_from_db()
            self.assertEqual((sale.card_amount or 0) + (sale.amount_paid or 0) - (sale.change_due or 0),
                             sale.total_amount, f'payment invariant broken on sale #{sale.pk} ({sale.payment_method})')

        self.assertEqual(self.qty(self.cola), D('45'))
        self.assertEqual(self.qty(self.cheese), D('4.900'))
        self.assertEqual(self.qty(self.cappuccino), D('0'), 'a recipe product never holds its own stock')
        self.assertEqual(self.qty(self.milk), D('9.550'), '3 cappuccinos x 0.150 kg milk')
        self.assertEqual(self.qty(self.coffee), D('1.970'), '3 cappuccinos x 0.010 kg coffee')

        self.login(self.manager)
        sales_rows = self.client.get(reverse('sales_report')).context['sales_data']
        self.assertEqual(len(sales_rows), 3)
        self.assertAlmostEqual(sum(r['total_amount'] for r in sales_rows), 9.60 + 10.50 + 2.00, places=2)

        qty_rows = {r['code']: r for r in self.client.get(reverse('sales_quantity_report')).context['sales_quantity_data']}
        self.assertAlmostEqual(qty_rows['1001']['quantity_sold'], 3)
        self.assertAlmostEqual(qty_rows['1001']['total_amount'], 6.00)
        self.assertAlmostEqual(qty_rows['1002']['quantity_sold'], 0.35)
        self.assertAlmostEqual(qty_rows['1005']['quantity_sold'], 3)

    def test_cashier_cannot_change_price_through_post(self):
        """The cart has no inline price editing and Edit Line's price is
        Manager-only -- the server must not trust a lower price posted by
        a modified page either."""
        self.receive_morning_delivery(timezone.localdate())
        sale = self.ring_up([{'product': self.cola, 'qty': 1, 'price': '0.01'}], method='CASH', paid=D('0.01'))
        item = sale.items.get()
        self.assertEqual(item.price_at_sale, D('2.00'),
                         'a cashier posted 0.01 for a 2.00 product and the server accepted it')
        self.assertEqual(SaleAttributes.objects.get(pk=sale.pk).total_amount, D('2.00'))

    def test_cashier_gets_active_promo_price(self):
        from STORA.pricelists.models import PriceList, PriceListRule
        today = timezone.localdate()
        promo = PriceList.objects.create(name='Promo', start_date=today, end_date=today, created_by=self.manager)
        PriceListRule.objects.create(price_list=promo, scope_type='PRODUCT', product=self.cola, fixed_price=D('1.50'))
        self.receive_morning_delivery(today)
        sale = self.ring_up([{'product': self.cola, 'qty': 2, 'price': '2.00'}], method='CARD')
        self.assertEqual(sale.items.get().price_at_sale, D('1.50'))

    def test_manager_can_override_price(self):
        self.receive_morning_delivery(timezone.localdate())
        self.login(self.manager)
        response = self.client.post(reverse('sale_add'), sale_post(
            [{'product': self.cola, 'qty': 1, 'price': '1.80'}], method='CARD'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SaleAttributes.objects.latest('pk').items.get().price_at_sale, D('1.80'))

    # ------------------------------------------------------------------
    # 3. Refunds
    # ------------------------------------------------------------------
    def test_partial_refund_returns_stock_and_blocks_over_refund(self):
        self.receive_morning_delivery(timezone.localdate())
        sale = self.ring_up([{'product': self.cola, 'qty': 4, 'price': '2.00'},
                             {'product': self.cappuccino, 'qty': 2, 'price': '3.50'}])
        cola_line = sale.items.get(sale_item=self.cola)
        capp_line = sale.items.get(sale_item=self.cappuccino)

        self.login(self.cashier)
        r = self.client.post(reverse('refund_new', args=[sale.pk]), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{cola_line.pk}': '1', f'refund_qty_{capp_line.pk}': '1',
        })
        self.assertEqual(r.status_code, 302)
        refund = RefundAttributes.objects.get()
        self.assertEqual(refund.total_amount, D('5.50'))
        self.assertEqual(self.qty(self.cola), D('45'))
        self.assertEqual(self.qty(self.milk), D('9.850'), '1 cappuccino refunded -> 0.150 kg milk back')

        r = self.client.post(reverse('refund_new', args=[sale.pk]), {
            'reason': RefundAttributes.RETURN_COMPLAINT, f'refund_qty_{cola_line.pk}': '4',
        })
        self.assertEqual(r.status_code, 200, 'refunding 4 more of a line with only 3 left must be refused')
        self.assertEqual(RefundAttributes.objects.count(), 1)

        self.login(self.manager)
        row = self.client.get(reverse('sales_report')).context['sales_data'][0]
        self.assertEqual(row['refund_status'], 'Partial')

    def test_deleting_refunded_sale_explains_instead_of_crashing(self):
        self.receive_morning_delivery(timezone.localdate())
        sale = self.ring_up([{'product': self.cola, 'qty': 2, 'price': '2.00'}])
        self.client.post(reverse('refund_new', args=[sale.pk]), {
            'reason': RefundAttributes.OPERATOR_ERROR, f'refund_qty_{sale.items.get().pk}': '1'})

        self.login(self.manager)
        response = self.client.post(reverse('sale_delete', args=[sale.pk]))
        self.assertEqual(response.status_code, 200, 'should re-render the confirm page, not 500')
        self.assertContains(response, 'has a refund linked to it')
        self.assertTrue(SaleAttributes.objects.filter(pk=sale.pk).exists())
        self.assertEqual(self.qty(self.cola), D('47'), 'stock must be untouched by the refused delete')

    def test_swapping_product_on_existing_sale_row_moves_stock_correctly(self):
        self.receive_morning_delivery(timezone.localdate())
        sale = self.ring_up([{'product': self.cola, 'qty': 2, 'price': '2.00'}])
        item = sale.items.get()
        item.sale_item = self.cappuccino  # e.g. corrected through the admin
        item.save()
        self.assertEqual(self.qty(self.cola), D('48'), 'cola is no longer on the sale -- both pieces come back')
        self.assertEqual(self.qty(self.milk), D('9.700'), '2 cappuccinos now consume 2 x 0.150 kg milk')

    # ------------------------------------------------------------------
    # 4. Write-off / scrap
    # ------------------------------------------------------------------
    def test_write_off_and_scrap_reduce_stock_and_stay_out_of_delivery_report(self):
        self.receive_morning_delivery(timezone.localdate())
        today = timezone.localdate()

        self.login(self.warehouse)
        r = self.client.post(reverse('writeoff_add'), delivery_post(
            self.supplier, None, '', today, [{'product': self.cola, 'qty': 6, 'price': '1.20'}]))
        self.assertEqual(r.status_code, 302)
        r = self.client.post(reverse('scrap_add'), delivery_post(
            None, None, '', today, [{'product': self.cheese, 'qty': '0.250', 'price': '10.90',
                                     'scrap_reason': self.expired}]))
        self.assertEqual(r.status_code, 302)

        self.assertEqual(self.qty(self.cola), D('42'))
        self.assertEqual(self.qty(self.cheese), D('5.000'))
        wo = DeliveryAttributes.objects.get(movement_type='WRITE_OFF')
        self.assertTrue(wo.document_number.startswith('WO-'))

        self.login(self.manager)
        deliveries = self.client.get(reverse('deliveries_report')).context['deliveries_data']
        self.assertEqual(len(deliveries), 1, 'write-off/scrap must not appear in the Delivery report')
        scraps = self.client.get(reverse('deliveries_report'), {'movement_type': 'SCRAP'}).context['deliveries_data']
        self.assertEqual(len(scraps), 1)

    # ------------------------------------------------------------------
    # 5. Revision
    # ------------------------------------------------------------------
    def test_revision_sets_stock_to_counted_quantity(self):
        self.receive_morning_delivery(timezone.localdate())
        self.login(self.warehouse)
        self.client.post(reverse('revision_start'), {'name': 'Monthly count'})
        revision = RevisionAttributes.objects.get(status='OPEN')
        self.client.post(reverse('revision_add_item', args=[revision.pk]), {'product_id': self.cola.pk, 'quantity': '40'})
        self.client.post(reverse('revision_add_item', args=[revision.pk]), {'product_id': self.cola.pk, 'quantity': '5'})
        self.client.post(reverse('revision_set_item', args=[revision.pk]), {'product_id': self.cheese.pk, 'quantity': '5.100'})
        self.client.post(reverse('revision_complete', args=[revision.pk]))

        self.assertEqual(self.qty(self.cola), D('45'), 'two scans of 40 and 5 should add up')
        self.assertEqual(self.qty(self.cheese), D('5.100'))
        self.assertEqual(self.qty(self.milk), D('10.000'), 'products not counted must not be touched')

    # ------------------------------------------------------------------
    # 6. Stock as of date -- must agree with what stock really was
    # ------------------------------------------------------------------
    def _backdate_everything_to(self, when):
        SaleAttributes.objects.update(time_of_sale=when)
        RefundAttributes.objects.update(time_of_refund=when)
        RevisionAttributes.objects.filter(status='COMPLETED').update(completed_at=when)

    def test_stock_as_of_yesterday_matches_real_end_of_day_stock(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        yesterday_noon = timezone.now() - timedelta(days=1)

        # --- Yesterday ---
        self.receive_morning_delivery(yesterday)
        self.ring_up([{'product': self.cola, 'qty': 5, 'price': '2.00'}])
        self._backdate_everything_to(yesterday_noon)
        end_of_yesterday = {p.pk: p.quantity for p in Product.objects.filter(is_recipe=False)}

        # --- Today: sale, recipe sale, refund, write-off, delivery, revision ---
        sale = self.ring_up([{'product': self.cola, 'qty': 3, 'price': '2.00'},
                             {'product': self.cappuccino, 'qty': 2, 'price': '3.50'}])
        self.login(self.cashier)
        self.client.post(reverse('refund_new', args=[sale.pk]), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{sale.items.get(sale_item=self.cola).pk}': '2',
            f'refund_qty_{sale.items.get(sale_item=self.cappuccino).pk}': '1',
        })
        self.assertEqual(RefundAttributes.objects.count(), 1)
        self.login(self.warehouse)
        self.client.post(reverse('writeoff_add'), delivery_post(
            self.supplier, None, '', timezone.localdate(), [{'product': self.cheese, 'qty': '1.000', 'price': '10.90'}]))
        self.client.post(reverse('delivery_add'), delivery_post(
            self.supplier2, self.invoice, 'INV-0002', timezone.localdate(), [{'product': self.milk, 'qty': '4', 'price': '1.80'}]))
        self.client.post(reverse('revision_start'), {'name': ''})
        revision = RevisionAttributes.objects.get(status='OPEN')
        self.client.post(reverse('revision_set_item', args=[revision.pk]), {'product_id': self.coffee.pk, 'quantity': '1.5'})
        self.client.post(reverse('revision_complete', args=[revision.pk]))

        as_of = {row['product'].pk: row['quantity_as_of'] for row in stock_as_of(yesterday)}
        names = {p.pk: p.name for p in Product.objects.all()}
        for pk, real in end_of_yesterday.items():
            self.assertEqual(as_of[pk], real,
                             f'"Stock as of {yesterday}" shows {as_of[pk]} for {names[pk]}, but at the end of that day there were really {real}')

    def test_stock_as_of_survives_sale_during_open_revision(self):
        """Revision started (snapshot taken), then a sale happens before the
        count is completed. Complete sets stock to the counted number --
        the report must undo what Complete ACTUALLY changed, not the
        difference against the earlier snapshot."""
        yesterday = timezone.localdate() - timedelta(days=1)
        self.receive_morning_delivery(yesterday)
        end_of_yesterday = self.qty(self.cola)  # 48

        self.login(self.warehouse)
        self.client.post(reverse('revision_start'), {'name': ''})
        revision = RevisionAttributes.objects.get(status='OPEN')
        self.client.post(reverse('revision_set_item', args=[revision.pk]), {'product_id': self.cola.pk, 'quantity': '46'})
        self.ring_up([{'product': self.cola, 'qty': 2, 'price': '2.00'}])  # sold while counting
        self.login(self.warehouse)
        self.client.post(reverse('revision_complete', args=[revision.pk]))
        self.assertEqual(self.qty(self.cola), D('46'))

        as_of = {row['product'].pk: row['quantity_as_of'] for row in stock_as_of(yesterday)}
        self.assertEqual(as_of[self.cola.pk], end_of_yesterday)

    def test_stock_balance_adds_up_with_sale_during_open_revision(self):
        """Opening + movements must equal closing even when a sale happens
        between counting a product and completing the revision."""
        from STORA.reports.services import stock_movement_totals
        self.receive_morning_delivery(timezone.localdate() - timedelta(days=1))
        self.login(self.warehouse)
        self.client.post(reverse('revision_start'), {'name': ''})
        revision = RevisionAttributes.objects.get(status='OPEN')
        self.client.post(reverse('revision_set_item', args=[revision.pk]), {'product_id': self.cola.pk, 'quantity': '46'})
        self.ring_up([{'product': self.cola, 'qty': 2, 'price': '2.00'}])
        self.login(self.warehouse)
        self.client.post(reverse('revision_complete', args=[revision.pk]))

        today = timezone.localdate()
        row = next(r for r in stock_movement_totals(today, today) if r['product'].pk == self.cola.pk)
        movements = (row['delivered'] - row['sold'] + row['refunded'] - row['scrapped']
                     - row['written_off'] + row['revised'] - row['recipe_consumed'])
        self.assertEqual(row['opening'], D('48'))
        self.assertEqual(row['revised'], D('0'), '46 counted, 46 really there after the sale -- no correction')
        self.assertEqual(row['opening'] + movements, row['closing'])

    # ------------------------------------------------------------------
    # 7. Expiring report
    # ------------------------------------------------------------------
    def test_expiring_report_lists_batch(self):
        self.receive_morning_delivery(timezone.localdate())
        self.login(self.manager)
        rows = self.client.get(reverse('expiring_report'), {'days_ahead': 30}).context['batches_data']
        self.assertEqual([r['code'] for r in rows], ['1001'])
        self.assertEqual(rows[0]['days_left'], 10)
        rows = self.client.get(reverse('expiring_report'), {'days_ahead': 5}).context['batches_data']
        self.assertEqual(rows, [])

    # ------------------------------------------------------------------
    # 8. Permissions per role on reports
    # ------------------------------------------------------------------
    def test_report_permissions_per_role(self):
        expectations = {
            'reports_dashboard': (200, 200, 200),
            'sales_report': (200, 403, 200),
            'deliveries_report': (200, 200, 403),
            'sales_quantity_report': (200, 200, 403),
            'stock_as_of_report': (200, 200, 200),
            'expiring_report': (200, 200, 200),
            'ai_report_list': (200, 403, 403),
        }
        for name, codes in expectations.items():
            for user, code in zip((self.manager, self.warehouse, self.cashier), codes):
                self.login(user)
                status = self.client.get(reverse(name)).status_code
                self.assertEqual(status, code, f'{user.role} on {name}: got {status}, expected {code}')

    # ------------------------------------------------------------------
    # 9. Smoke: every list/detail screen renders for a Manager with data
    # ------------------------------------------------------------------
    def test_every_screen_renders_with_real_data(self):
        delivery = self.receive_morning_delivery(timezone.localdate())
        sale = self.ring_up([{'product': self.cola, 'qty': 1, 'price': '2.00'}])
        self.login(self.cashier)
        self.client.post(reverse('refund_new', args=[sale.pk]), {
            'reason': RefundAttributes.OPERATOR_ERROR, f'refund_qty_{sale.items.get().pk}': '1'})
        refund = RefundAttributes.objects.get()
        self.login(self.warehouse)
        self.client.post(reverse('revision_start'), {'name': 'Smoke'})
        revision = RevisionAttributes.objects.get()

        self.login(self.manager)
        urls = [
            reverse('index'), reverse('product_list'), reverse('product_create'),
            reverse('product_details', args=[self.cola.pk]), reverse('product_edit', args=[self.cola.pk]),
            reverse('product_history', args=[self.cola.pk]), reverse('product_details', args=[self.cappuccino.pk]),
            reverse('category_list'), reverse('category_create'), reverse('tax_group_list'),
            reverse('suppliers_list'), reverse('suppliers_create'), reverse('employee_list'),
            reverse('employee_details', args=[self.cashier.pk]), reverse('company_profile_edit'),
            reverse('sale_add'), reverse('sale_details', args=[sale.pk]), reverse('refund_find'),
            reverse('refund_new', args=[sale.pk]), reverse('refund_details', args=[refund.pk]),
            reverse('fiscal_reports'), reverse('deliveries_list'), reverse('delivery_add'), reverse('writeoff_add'),
            reverse('scrap_add'), reverse('delivery_details', args=[delivery.pk]),
            reverse('delivery_edit', args=[delivery.pk]), reverse('document_type_list'), reverse('scrap_reason_list'),
            reverse('reports_dashboard'), reverse('sales_report'), reverse('deliveries_report'),
            reverse('stock_as_of_report'), reverse('expiring_report'), reverse('sales_quantity_report'),
            reverse('ai_report_list'), reverse('revision_home'), reverse('revision_list'),
            reverse('revision_view', args=[revision.pk]), reverse('price_list_list'), reverse('price_list_create'),
            reverse('order_list'), reverse('order_new'), reverse('global_search') + '?q=cola',
            reverse('ingredient_search') + '?q=milk',
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertIn(response.status_code, (200, 302), f'{url} -> {response.status_code}')
