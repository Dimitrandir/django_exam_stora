import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from django.utils import timezone

from STORA.products.models import Category, Product, RecipeIngredient
from STORA.pricelists.models import PriceList, PriceListRule
from STORA.sales.models import SaleAttributes, SaleItems, PosPin, SaleItemVoidLog, RefundAttributes, RefundItems
from STORA.sales.forms import SaleItemForm
from STORA.sales.tasks import backfill_recipe_ingredient_stock


User = get_user_model()


class SalesViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='cashier',
            password='pass12345',
        )
        self.client.login(username='cashier', password='pass12345')

        self.product = Product.objects.create(
            internal_code='P0000003',
            name='Sale Product',
            delivery_price=2.00,
            sell_price=3.00,
            quantity=10,
        )

    def test_sales_add_page_loads(self):
        response = self.client.get(reverse('sale_add'))
        self.assertEqual(response.status_code, 200)

    def test_sale_details_page_loads(self):
        sale = SaleAttributes.objects.create(cashier=self.user)
        response = self.client.get(reverse('sale_details', kwargs={'pk': sale.pk}))
        self.assertEqual(response.status_code, 200)

    def test_sale_delete_page_loads(self):
        sale = SaleAttributes.objects.create(cashier=self.user)
        response = self.client.get(reverse('sale_delete', kwargs={'pk': sale.pk}))
        self.assertEqual(response.status_code, 200)

    def test_sale_item_model_can_be_created(self):
        sale = SaleAttributes.objects.create(cashier=self.user)
        item = SaleItems.objects.create(
            sale=sale,
            sale_item=self.product,
            sale_quantity=2,
            price_at_sale=3.00,
            total_price_row=6.00,
        )
        self.assertEqual(item.total_price_row, 6.00)

    def test_sale_add_page_uses_template(self):
        response = self.client.get(reverse('sale_add'))
        self.assertTemplateUsed(response, 'sales/sale_add.html')

    def test_sale_item_accepts_fractional_quantity_for_weight_products(self):
        weight_product = Product.objects.create(
            internal_code='P0000005', name='Loose Peaches', sell_price=2.5, quantity=Decimal('10.000'),
            unit_type=Product.WEIGHT,
        )
        sale = SaleAttributes.objects.create(cashier=self.user)
        SaleItems.objects.create(sale=sale, sale_item=weight_product, sale_quantity=Decimal('0.350'))

        weight_product.refresh_from_db()
        self.assertEqual(weight_product.quantity, Decimal('9.650'))


class SalesPermissionTests(TestCase):
    """Every sales view used to have zero login/permission checks -- wide
    open to an anonymous visitor. Cashiers/Managers groups already had the
    right permissions assigned in accounts/signals.py, they just were never
    checked anywhere."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='perm-cashier', password='pass12345', role=User.CASHIER)
        self.warehouse = User.objects.create_user(username='perm-warehouse', password='pass12345', role=User.WAREHOUSE)
        self.manager = User.objects.create_user(username='perm-manager', password='pass12345', role=User.MANAGER)
        self.sale = SaleAttributes.objects.create(cashier=self.cashier)

    def test_anonymous_is_redirected_to_login_not_500(self):
        for url in [reverse('sale_add'), reverse('sale_details', args=[self.sale.pk]),
                    reverse('sale_delete', args=[self.sale.pk])]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn('/accounts/login/', response.url, url)

    def test_warehouse_has_no_sales_access(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('sale_add')).status_code, 403)
        self.assertEqual(self.client.get(reverse('sale_details', args=[self.sale.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('sale_delete', args=[self.sale.pk])).status_code, 403)

    def test_cashier_has_full_sales_access(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse('sale_add')).status_code, 200)
        self.assertEqual(self.client.get(reverse('sale_details', args=[self.sale.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('sale_delete', args=[self.sale.pk])).status_code, 200)

    def test_manager_has_full_sales_access(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('sale_add')).status_code, 200)


class SalesFormSubmissionTests(TestCase):
    """Full POST cycle through sales_add -- also pins down that removing
    the redundant second `sale.save()` call didn't break total_amount
    (SaleItems.save()'s post_save signal sets it on this exact same `sale`
    object via the formset's shared instance, before the single remaining
    save)."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='submit-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.product = Product.objects.create(
            internal_code='P0000011', name='Cookies', sell_price=Decimal('2.50'), quantity=Decimal('20.000'),
        )

    def test_creating_sale_with_one_item_sets_total_amount(self):
        data = {
            'cashier': self.cashier.pk,
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-sale_item': self.product.pk,
            'items-0-sale_quantity': '4',
            'items-0-price_at_sale': '2.50',
            'items-0-total_price_row': '10.00',
            'items-0-DELETE': '',
        }
        response = self.client.post(reverse('sale_add'), data)
        self.assertRedirects(response, reverse('sale_add'))

        sale = SaleAttributes.objects.get(cashier=self.cashier)
        self.assertEqual(sale.total_amount, Decimal('10.00'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('16.000'))

    def test_creating_sale_with_checkout_payment_fields(self):
        # What the checkout modal (Фаза 2 step 3) actually posts -- the
        # hidden payment_method/amount_paid/change_due fields it fills in
        # right before calling requestSubmit().
        data = {
            'cashier': self.cashier.pk,
            'payment_method': SaleAttributes.CASH,
            'amount_paid': '20.00',
            'change_due': '10.00',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-sale_item': self.product.pk,
            'items-0-sale_quantity': '4',
            'items-0-price_at_sale': '2.50',
            'items-0-total_price_row': '10.00',
            'items-0-DELETE': '',
        }
        response = self.client.post(reverse('sale_add'), data)
        self.assertRedirects(response, reverse('sale_add'))

        sale = SaleAttributes.objects.get(cashier=self.cashier)
        self.assertEqual(sale.payment_method, SaleAttributes.CASH)
        self.assertEqual(sale.amount_paid, Decimal('20.00'))
        self.assertEqual(sale.change_due, Decimal('10.00'))

    def test_creating_sale_with_mixed_payment(self):
        # Split payment: part card, the remainder in cash (assumed exact,
        # no change) -- what the checkout modal posts when "Mixed" is
        # selected.
        data = {
            'cashier': self.cashier.pk,
            'payment_method': SaleAttributes.MIXED,
            'amount_paid': '4.00',
            'card_amount': '6.00',
            'change_due': '0.00',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-sale_item': self.product.pk,
            'items-0-sale_quantity': '4',
            'items-0-price_at_sale': '2.50',
            'items-0-total_price_row': '10.00',
            'items-0-DELETE': '',
        }
        response = self.client.post(reverse('sale_add'), data)
        self.assertRedirects(response, reverse('sale_add'))

        sale = SaleAttributes.objects.get(cashier=self.cashier)
        self.assertEqual(sale.payment_method, SaleAttributes.MIXED)
        self.assertEqual(sale.amount_paid, Decimal('4.00'))
        self.assertEqual(sale.card_amount, Decimal('6.00'))
        self.assertEqual(sale.card_amount + sale.amount_paid, sale.total_amount)


class SalePaymentFieldsTests(TestCase):
    """SaleAttributes.payment_method/amount_paid/change_due (Фаза 2 step 1,
    checkout modal prep) -- nullable so existing/pre-checkout-screen sales
    aren't affected."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='payment-cashier', password='pass12345', role=User.CASHIER)

    def test_defaults_to_null(self):
        sale = SaleAttributes.objects.create(cashier=self.cashier)
        self.assertIsNone(sale.payment_method)
        self.assertIsNone(sale.amount_paid)
        self.assertIsNone(sale.card_amount)
        self.assertIsNone(sale.change_due)

    def test_stores_cash_payment_with_change(self):
        sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH,
            amount_paid=Decimal('10.00'), change_due=Decimal('2.50'),
        )
        self.assertEqual(sale.payment_method, SaleAttributes.CASH)
        self.assertEqual(sale.change_due, Decimal('2.50'))

    def test_stores_mixed_payment_split_between_cash_and_card(self):
        sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.MIXED,
            amount_paid=Decimal('4.00'), card_amount=Decimal('6.00'), change_due=Decimal('0.00'),
        )
        self.assertEqual(sale.payment_method, SaleAttributes.MIXED)
        self.assertEqual(sale.card_amount, Decimal('6.00'))


class SalesAddCategoryPanelDataTests(TestCase):
    """sales_add's context feeds the category/subcategory quick-pick panel
    (Фаза 2 step 2) -- products carry category_id, and categories_data has
    the full tree (id/name/parent_id) for the JS to build folders from."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='panel-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.drinks = Category.objects.create(name='Panel Drinks')
        self.sodas = Category.objects.create(name='Panel Sodas', parent=self.drinks)
        self.product = Product.objects.create(
            internal_code='PNL0001', name='Panel Cola', sell_price=Decimal('1.50'), quantity=10,
            category=self.drinks,
        )

    def test_products_data_includes_category_id(self):
        response = self.client.get(reverse('sale_add'))
        row = next(p for p in response.context['products_data'] if p['id'] == self.product.pk)
        self.assertEqual(row['category_id'], self.drinks.pk)

    def test_categories_data_includes_parent_relationship(self):
        response = self.client.get(reverse('sale_add'))
        by_id = {c['id']: c for c in response.context['categories_data']}
        self.assertIsNone(by_id[self.drinks.pk]['parent_id'])
        self.assertEqual(by_id[self.sodas.pk]['parent_id'], self.drinks.pk)


class PosPriceListIntegrationTests(TestCase):
    """The cart/search JS reads sell_price straight off products_data --
    when an active price list rule covers a product, that field must
    already be the discounted price, not the catalog one (see
    STORA.pricelists.services.resolve_prices)."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='promo-cashier', password='pass12345', role=User.CASHIER)
        self.manager = User.objects.create_user(username='promo-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.cashier)
        self.product = Product.objects.create(
            internal_code='PROMO001', name='Promo Cola', sell_price=Decimal('2.00'), quantity=10,
        )

    def test_products_data_uses_regular_price_with_no_active_list(self):
        response = self.client.get(reverse('sale_add'))
        row = next(p for p in response.context['products_data'] if p['id'] == self.product.pk)
        self.assertEqual(row['sell_price'], 2.00)

    def test_products_data_uses_discounted_price_when_a_list_is_active(self):
        today = timezone.localdate()
        price_list = PriceList.objects.create(
            name='Test Promo', start_date=today - timedelta(days=1), end_date=today + timedelta(days=1),
            created_by=self.manager,
        )
        PriceListRule.objects.create(
            price_list=price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
            product=self.product, discount_percent=Decimal('25'),
        )
        response = self.client.get(reverse('sale_add'))
        row = next(p for p in response.context['products_data'] if p['id'] == self.product.pk)
        self.assertEqual(row['sell_price'], 1.50)


class PosShowOnPosDataTests(TestCase):
    """Category.show_on_pos/Product.show_on_pos control the POS screen's
    category quick-pick panel -- sales_add's context carries the flag for
    both, and the full (unfiltered) products_data stays available so
    findProductByCode (barcode/code search) keeps working for every
    product regardless of this flag."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='pos-flag-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.visible_category = Category.objects.create(name='Visible Category', show_on_pos=True)
        self.hidden_category = Category.objects.create(name='Hidden Category', show_on_pos=False)
        self.visible_product = Product.objects.create(
            internal_code='POS0001', name='Visible Product', sell_price=Decimal('2.00'), quantity=10,
            category=self.visible_category, show_on_pos=True,
        )
        self.hidden_product = Product.objects.create(
            internal_code='POS0002', name='Hidden Product', sell_price=Decimal('2.00'), quantity=10,
            category=self.visible_category, show_on_pos=False,
        )

    def test_categories_data_carries_show_on_pos(self):
        response = self.client.get(reverse('sale_add'))
        by_id = {c['id']: c for c in response.context['categories_data']}
        self.assertTrue(by_id[self.visible_category.pk]['show_on_pos'])
        self.assertFalse(by_id[self.hidden_category.pk]['show_on_pos'])

    def test_products_data_carries_show_on_pos_but_is_not_filtered(self):
        # The view must NOT drop hidden products from products_data -- the
        # barcode/code search box still needs to find them.
        response = self.client.get(reverse('sale_add'))
        by_id = {p['id']: p for p in response.context['products_data']}
        self.assertTrue(by_id[self.visible_product.pk]['show_on_pos'])
        self.assertFalse(by_id[self.hidden_product.pk]['show_on_pos'])

    def test_new_category_and_product_default_to_hidden(self):
        self.assertFalse(Category.objects.create(name='Brand New Category').show_on_pos)
        self.assertFalse(
            Product.objects.create(
                internal_code='POS0003', name='Brand New Product', sell_price=Decimal('1.00'),
            ).show_on_pos
        )


class PosRecentSalesRankingTests(TestCase):
    """products_data carries recent_qty (total sold in the last
    POS_RECENT_SALES_DAYS days) -- the category panel's JS uses it to put
    the most-recently-popular products in the first slots when a category
    is opened. Sales older than the window don't count."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='pos-recent-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Recent Sales Category', show_on_pos=True)
        self.popular = Product.objects.create(
            internal_code='REC0001', name='Popular Product', sell_price=Decimal('1.00'), quantity=100,
            category=self.category, show_on_pos=True,
        )
        self.quiet = Product.objects.create(
            internal_code='REC0002', name='Quiet Product', sell_price=Decimal('1.00'), quantity=100,
            category=self.category, show_on_pos=True,
        )
        sale = SaleAttributes.objects.create(cashier=self.cashier)
        SaleItems.objects.create(sale=sale, sale_item=self.popular, sale_quantity=Decimal('5'), price_at_sale=Decimal('1.00'))

        old_sale = SaleAttributes.objects.create(cashier=self.cashier)
        old_item = SaleItems.objects.create(sale=old_sale, sale_item=self.quiet, sale_quantity=Decimal('9'), price_at_sale=Decimal('1.00'))
        # Back-date the old sale past the recent-sales window -- update()
        # bypasses auto_now_add so this sticks.
        SaleAttributes.objects.filter(pk=old_sale.pk).update(time_of_sale=timezone.now() - timezone.timedelta(days=30))

    def test_recent_qty_counts_only_sales_within_the_window(self):
        response = self.client.get(reverse('sale_add'))
        by_id = {p['id']: p for p in response.context['products_data']}
        self.assertEqual(by_id[self.popular.pk]['recent_qty'], 5.0)
        self.assertEqual(by_id[self.quiet.pk]['recent_qty'], 0.0)


class SaleItemsInitialDraftRestoreTests(TestCase):
    """sales_add seeds the Tabulator cart (sale_items_initial) by resolving
    a saved draft's rows back to their real Product -- covers the Стъпка 4
    rewrite (plain table -> Tabulator). A weight product's row must carry
    unit_type, or the Qty column's numberEditor falls back to whole-piece
    step/min and rejects a fractional value like 0.350 on restore."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='draft-restore-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.weight_product = Product.objects.create(
            internal_code='DRAFT001', name='Draft Weight Product', unit_type=Product.WEIGHT,
            sell_price=Decimal('4.00'), quantity=10,
        )

    def _set_draft(self, forms):
        from STORA.sales.tab_state import DEFAULT_TAB, SALE_TABS_SESSION_KEY

        session = self.client.session
        session[SALE_TABS_SESSION_KEY] = {DEFAULT_TAB: {'formset_data': {'forms': forms}, 'active': True}}
        session.save()

    def test_restored_row_carries_unit_type_for_weight_product(self):
        self._set_draft([{
            'sale_item': str(self.weight_product.pk), 'sale_quantity': '0.35',
            'price_at_sale': '4.00', 'total_price_row': '1.40', 'DELETE': '',
        }])
        response = self.client.get(reverse('sale_add'))
        rows = response.context['sale_items_initial']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['unit_type'], Product.WEIGHT)
        self.assertEqual(rows[0]['sale_quantity'], 0.35)

    def test_deleted_draft_row_is_skipped(self):
        self._set_draft([{
            'sale_item': str(self.weight_product.pk), 'sale_quantity': '1',
            'price_at_sale': '4.00', 'total_price_row': '4.00', 'DELETE': 'on',
        }])
        response = self.client.get(reverse('sale_add'))
        self.assertEqual(response.context['sale_items_initial'], [])


class SaleTabsTests(TestCase):
    """The 3 basket tabs on the POS screen each keep an independent draft
    (see STORA.sales.tab_state) -- separate from the single-slot mechanism
    deliveries/write-off/scrap share, since only sales needed more than one
    at a time."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='tabs-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.product_a = Product.objects.create(internal_code='TAB0001', name='Tab Product A', sell_price=Decimal('2.00'), quantity=10)
        self.product_b = Product.objects.create(internal_code='TAB0002', name='Tab Product B', sell_price=Decimal('3.00'), quantity=10)

    def _save_draft(self, product):
        return self.client.post(
            reverse('sale_draft_save'),
            data=json.dumps({
                'form_data': {},
                'formset_data': {'forms': [{
                    'sale_item': str(product.pk), 'sale_quantity': '1',
                    'price_at_sale': str(product.sell_price), 'total_price_row': str(product.sell_price),
                    'DELETE': '',
                }]},
            }),
            content_type='application/json',
        )

    def test_defaults_to_tab_1(self):
        response = self.client.get(reverse('sale_add'))
        self.assertEqual(response.context['active_tab'], '1')

    def test_draft_saved_on_one_tab_does_not_leak_into_another(self):
        self._save_draft(self.product_a)  # lands on tab 1 (the default)

        self.client.post(reverse('switch_sale_tab'), data=json.dumps({'tab': '2'}), content_type='application/json')
        response = self.client.get(reverse('sale_add'))
        self.assertEqual(response.context['active_tab'], '2')
        self.assertEqual(response.context['sale_items_initial'], [])  # tab 2 starts empty

        self.client.post(reverse('switch_sale_tab'), data=json.dumps({'tab': '1'}), content_type='application/json')
        response = self.client.get(reverse('sale_add'))
        rows = response.context['sale_items_initial']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sale_item'], self.product_a.pk)

    def test_tabs_summary_reflects_which_tabs_have_items(self):
        self._save_draft(self.product_a)  # tab 1
        self.client.post(reverse('switch_sale_tab'), data=json.dumps({'tab': '3'}), content_type='application/json')
        self._save_draft(self.product_b)  # tab 3

        response = self.client.get(reverse('sale_add'))
        summary = {row['tab']: row for row in response.context['sale_tabs_summary']}
        self.assertTrue(summary['1']['has_items'])
        self.assertFalse(summary['2']['has_items'])
        self.assertTrue(summary['3']['has_items'])
        self.assertTrue(summary['3']['is_active'])
        self.assertFalse(summary['1']['is_active'])

    def test_switch_tab_rejects_invalid_tab(self):
        response = self.client.post(reverse('switch_sale_tab'), data=json.dumps({'tab': '99'}), content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_completing_a_sale_clears_only_that_tabs_draft(self):
        self._save_draft(self.product_a)  # tab 1
        self.client.post(reverse('switch_sale_tab'), data=json.dumps({'tab': '2'}), content_type='application/json')
        self._save_draft(self.product_b)  # tab 2

        # Complete the sale sitting on tab 2.
        self.client.post(reverse('sale_add'), {
            'cashier': self.cashier.pk,
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-sale_item': self.product_b.pk, 'items-0-sale_quantity': '1',
            'items-0-price_at_sale': '3.00', 'items-0-total_price_row': '3.00', 'items-0-DELETE': '',
        })

        response = self.client.get(reverse('sale_add'))
        self.assertEqual(response.context['active_tab'], '2')  # stays on tab 2
        self.assertEqual(response.context['sale_items_initial'], [])  # now empty

        self.client.post(reverse('switch_sale_tab'), data=json.dumps({'tab': '1'}), content_type='application/json')
        response = self.client.get(reverse('sale_add'))
        self.assertEqual(len(response.context['sale_items_initial']), 1)  # tab 1's draft untouched

    def test_ghost_draft_row_does_not_trip_validation_on_a_plain_page_load(self):
        # A stray draft-save with no product attached (sale_item empty) --
        # whatever caused it, it must never be restored as if it were a
        # real cart line. Before the fix, GET unconditionally bound the
        # formset from ANY "active" draft, so BaseSaleItemFormSet.clean()'s
        # "must add at least one sale item" fired on a plain page load,
        # before the cashier had touched anything.
        self.client.post(
            reverse('sale_draft_save'),
            data=json.dumps({'formset_data': {'forms': [{
                'sale_item': '', 'sale_quantity': '1', 'product_name': '',
                'price_at_sale': '0', 'total_price_row': '0', 'DELETE': '',
            }]}}),
            content_type='application/json',
        )

        response = self.client.get(reverse('sale_add'))
        self.assertFalse(response.context['formset'].non_form_errors())
        self.assertEqual(response.context['sale_items_initial'], [])
        # Self-healing -- the ghost draft doesn't linger to break the next
        # load too.
        self.assertIsNone(self.client.get(reverse('sale_add')).context.get('sale_draft'))

    def test_cancel_sale_clears_active_tab_and_redirects_to_sale_add(self):
        self._save_draft(self.product_a)
        response = self.client.get(reverse('cancel_sale'))
        self.assertRedirects(response, reverse('sale_add'))
        self.assertEqual(self.client.get(reverse('sale_add')).context['sale_items_initial'], [])

    def test_completing_a_sale_persists_change_until_next_item_added(self):
        # Cash sale for 2.00, paid with a fiver -- 3.00 change.
        self.client.post(reverse('sale_add'), {
            'cashier': self.cashier.pk,
            'payment_method': 'CASH', 'amount_paid': '5.00', 'card_amount': '0.00', 'change_due': '3.00',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-sale_item': self.product_a.pk, 'items-0-sale_quantity': '1',
            'items-0-price_at_sale': '2.00', 'items-0-total_price_row': '2.00', 'items-0-DELETE': '',
        })

        # Still there on the next fresh-cart page load.
        response = self.client.get(reverse('sale_add'))
        self.assertEqual(response.context['last_change'], '3.00')

        # Ringing up the next customer (any cart activity) clears it --
        # see clear_last_change in sales_draft_save.
        self._save_draft(self.product_b)
        response = self.client.get(reverse('sale_add'))
        self.assertIsNone(response.context['last_change'])


class PosPinViewTests(TestCase):
    """PosPin rows (the fixed shortcut bar's contents) are managed through
    two plain POST endpoints reached from the POS screen's own "edit
    shortcuts" mode -- see sale_add.html. Only show_on_pos items can be
    pinned; pos_pins_data in sales_add's context reflects current pins."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='pos-pin-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.category = Category.objects.create(name='Pinnable Category', show_on_pos=True)
        self.hidden_category = Category.objects.create(name='Unpinnable Category', show_on_pos=False)
        self.product = Product.objects.create(
            internal_code='PIN0001', name='Pinnable Product', sell_price=Decimal('1.00'), quantity=10,
            show_on_pos=True,
        )

    def test_pos_pins_data_reflects_existing_pins_in_position_order(self):
        PosPin.objects.create(product=self.product, position=2)
        PosPin.objects.create(category=self.category, position=1)

        response = self.client.get(reverse('sale_add'))
        pins = response.context['pos_pins_data']
        self.assertEqual([p['type'] for p in pins], ['category', 'product'])
        self.assertEqual(pins[0]['name'], self.category.name)

    def test_add_category_pin(self):
        response = self.client.post(
            reverse('pos_pin_add'), data=json.dumps({'type': 'category', 'target_id': self.category.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        pin = PosPin.objects.get()
        self.assertEqual(pin.category_id, self.category.pk)
        self.assertIsNone(pin.product_id)

    def test_add_product_pin(self):
        response = self.client.post(
            reverse('pos_pin_add'), data=json.dumps({'type': 'product', 'target_id': self.product.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        pin = PosPin.objects.get()
        self.assertEqual(pin.product_id, self.product.pk)
        self.assertIsNone(pin.category_id)

    def test_cannot_pin_a_category_that_is_not_show_on_pos(self):
        response = self.client.post(
            reverse('pos_pin_add'), data=json.dumps({'type': 'category', 'target_id': self.hidden_category.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(PosPin.objects.exists())

    def test_remove_pin(self):
        pin = PosPin.objects.create(product=self.product, position=1)
        response = self.client.post(reverse('pos_pin_remove', kwargs={'pk': pin.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PosPin.objects.exists())

    def test_pin_endpoints_require_login(self):
        self.client.logout()
        response = self.client.post(
            reverse('pos_pin_add'), data=json.dumps({'type': 'product', 'target_id': self.product.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 302)


class LogRemovedSaleItemViewTests(TestCase):
    """Deleting a line (or voiding the whole cart) on the POS screen
    happens before any SaleAttributes row exists -- this is the only
    record of it, written via a plain POST from sale_add.html's JS."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='void-log-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.product = Product.objects.create(
            internal_code='VOID0001', name='Void Log Product', sell_price=Decimal('3.00'), quantity=10,
        )

    def test_log_single_removed_line(self):
        response = self.client.post(
            reverse('log_removed_sale_item'),
            data=json.dumps({
                'action': 'REMOVE_LINE',
                'items': [{'product_id': self.product.pk, 'quantity': 2, 'unit_price': '3.00', 'total_price': '6.00'}],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        entry = SaleItemVoidLog.objects.get()
        self.assertEqual(entry.employee, self.cashier)
        self.assertEqual(entry.product, self.product)
        self.assertEqual(entry.quantity, Decimal('2'))
        self.assertEqual(entry.action, SaleItemVoidLog.REMOVE_LINE)

    def test_log_void_whole_sale_with_multiple_items(self):
        other = Product.objects.create(internal_code='VOID0002', name='Void Log Product 2', sell_price=Decimal('1.00'), quantity=5)
        response = self.client.post(
            reverse('log_removed_sale_item'),
            data=json.dumps({
                'action': 'VOID_SALE',
                'items': [
                    {'product_id': self.product.pk, 'quantity': 1, 'unit_price': '3.00', 'total_price': '3.00'},
                    {'product_id': other.pk, 'quantity': 4, 'unit_price': '1.00', 'total_price': '4.00'},
                ],
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SaleItemVoidLog.objects.filter(action=SaleItemVoidLog.VOID_SALE).count(), 2)

    def test_invalid_action_rejected(self):
        response = self.client.post(
            reverse('log_removed_sale_item'),
            data=json.dumps({'action': 'NOT_A_REAL_ACTION', 'items': []}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SaleItemVoidLog.objects.exists())

    def test_requires_login(self):
        self.client.logout()
        response = self.client.post(
            reverse('log_removed_sale_item'),
            data=json.dumps({'action': 'REMOVE_LINE', 'items': []}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 302)


class SaleFormValidationTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            internal_code='P0000004',
            name='Validation Product',
            delivery_price=2.00,
            sell_price=3.00,
            quantity=10,
        )

    def test_sale_item_form_requires_quantity(self):
        form_data = {
            'sale_item': self.product.pk,
            'sale_quantity': '',
            'price_at_sale': '3.00',
            'total_price_row': '6.00',
        }
        form = SaleItemForm(data=form_data)
        self.assertFalse(form.is_valid())

    def test_sale_item_form_requires_item(self):
        form_data = {
            'sale_item': '',
            'sale_quantity': '2',
            'price_at_sale': '3.00',
            'total_price_row': '6.00',
        }
        form = SaleItemForm(data=form_data)
        self.assertFalse(form.is_valid())


class RecipeStockDeductionTests(TestCase):
    """Selling a recipe product (e.g. Cappuccino = 0.050kg milk + 0.007kg
    coffee) must deduct ITS INGREDIENTS' stock, not its own (nonexistent)
    quantity."""

    def setUp(self):
        self.user = User.objects.create_user(username='cashier2', password='pass12345')
        self.milk = Product.objects.create(
            internal_code='RS000001', name='Milk', sell_price=2, quantity=Decimal('5.000'),
            unit_type=Product.WEIGHT,
        )
        self.coffee = Product.objects.create(
            internal_code='RS000002', name='Coffee Beans', sell_price=20, quantity=Decimal('2.000'),
            unit_type=Product.WEIGHT,
        )
        self.cappuccino = Product.objects.create(
            internal_code='RS000003', name='Cappuccino', sell_price=3, quantity=0, is_recipe=True,
        )
        RecipeIngredient.objects.create(recipe=self.cappuccino, ingredient=self.milk, quantity=Decimal('0.050'))
        RecipeIngredient.objects.create(recipe=self.cappuccino, ingredient=self.coffee, quantity=Decimal('0.007'))

    def test_selling_recipe_product_deducts_ingredients_not_own_stock(self):
        sale = SaleAttributes.objects.create(cashier=self.user)
        SaleItems.objects.create(sale=sale, sale_item=self.cappuccino, sale_quantity=2)

        self.milk.refresh_from_db()
        self.coffee.refresh_from_db()
        self.cappuccino.refresh_from_db()
        self.assertEqual(self.milk.quantity, Decimal('4.900'))
        self.assertEqual(self.coffee.quantity, Decimal('1.986'))
        self.assertEqual(self.cappuccino.quantity, 0)

    def test_editing_recipe_sale_item_applies_only_the_delta(self):
        sale = SaleAttributes.objects.create(cashier=self.user)
        item = SaleItems.objects.create(sale=sale, sale_item=self.cappuccino, sale_quantity=1)
        item.sale_quantity = 3
        item.save()

        self.milk.refresh_from_db()
        self.assertEqual(self.milk.quantity, Decimal('5.000') - Decimal('0.050') * 3)

    def test_deleting_recipe_sale_item_restores_ingredient_stock(self):
        sale = SaleAttributes.objects.create(cashier=self.user)
        item = SaleItems.objects.create(sale=sale, sale_item=self.cappuccino, sale_quantity=2)
        item.delete()

        self.milk.refresh_from_db()
        self.coffee.refresh_from_db()
        self.assertEqual(self.milk.quantity, Decimal('5.000'))
        self.assertEqual(self.coffee.quantity, Decimal('2.000'))

    def test_recipe_with_no_ingredients_defined_does_not_error(self):
        empty_recipe = Product.objects.create(
            internal_code='RS000004', name='Undefined Recipe', sell_price=1, quantity=0, is_recipe=True,
        )
        sale = SaleAttributes.objects.create(cashier=self.user)
        SaleItems.objects.create(sale=sale, sale_item=empty_recipe, sale_quantity=1)


class RecipeBackfillTaskTests(TestCase):
    """A product can be sold as a plain item for a while and only later get
    marked as a recipe -- `backfill_recipe_ingredient_stock` must catch up
    ingredient stock for those past sales, exactly once."""

    def setUp(self):
        self.user = User.objects.create_user(username='cashier3', password='pass12345')
        self.milk = Product.objects.create(
            internal_code='RB000001', name='Milk', sell_price=2, quantity=Decimal('5.000'),
            unit_type=Product.WEIGHT,
        )
        self.cappuccino = Product.objects.create(
            internal_code='RB000002', name='Cappuccino', sell_price=3, quantity=Decimal('50.000'),
        )

        sale = SaleAttributes.objects.create(cashier=self.user)
        # Sold as a plain product BEFORE it became a recipe -- this deducted
        # from the cappuccino's own (now-irrelevant) stock, not the milk.
        SaleItems.objects.create(sale=sale, sale_item=self.cappuccino, sale_quantity=3)

        self.cappuccino.refresh_from_db()
        self.cappuccino.is_recipe = True
        self.cappuccino.save(update_fields=['is_recipe'])
        RecipeIngredient.objects.create(recipe=self.cappuccino, ingredient=self.milk, quantity=Decimal('0.050'))

    def test_backfill_deducts_ingredients_for_historical_sales(self):
        backfill_recipe_ingredient_stock(self.cappuccino.pk)

        self.milk.refresh_from_db()
        self.cappuccino.refresh_from_db()
        # 3 past cappuccinos x 0.050kg milk each = 0.150kg deducted.
        self.assertEqual(self.milk.quantity, Decimal('4.850'))
        self.assertIsNotNone(self.cappuccino.ingredients_backfilled_at)

    def test_backfill_is_idempotent(self):
        backfill_recipe_ingredient_stock(self.cappuccino.pk)
        backfill_recipe_ingredient_stock(self.cappuccino.pk)

        self.milk.refresh_from_db()
        self.assertEqual(self.milk.quantity, Decimal('4.850'))

    def test_backfill_noop_for_non_recipe_product(self):
        plain_product = Product.objects.create(
            internal_code='RB000003', name='Plain Widget', sell_price=1, quantity=10,
        )
        backfill_recipe_ingredient_stock(plain_product.pk)

        plain_product.refresh_from_db()
        self.assertIsNone(plain_product.ingredients_backfilled_at)


class RefundViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='refund-cashier', password='pass12345')
        self.client.login(username='refund-cashier', password='pass12345')

        self.product = Product.objects.create(
            internal_code='RF000001', name='Refund Product', sell_price=Decimal('4.00'), quantity=10,
        )
        self.sale = SaleAttributes.objects.create(cashier=self.user)
        self.item = SaleItems.objects.create(
            sale=self.sale, sale_item=self.product, sale_quantity=Decimal('3.000'), price_at_sale=Decimal('4.00'),
        )
        # Stock after the sale: 10 - 3 = 7.
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 7)

    def test_refund_find_page_loads(self):
        response = self.client.get(reverse('refund_find'))
        self.assertEqual(response.status_code, 200)

    def test_refund_find_locates_sale_by_id(self):
        response = self.client.get(reverse('refund_find'), {'q': str(self.sale.pk)})
        self.assertIn(self.sale, response.context['results'])

    def test_refund_find_shows_recent_receipts_with_no_filters(self):
        response = self.client.get(reverse('refund_find'))
        self.assertFalse(response.context['any_filter'])
        self.assertIn(self.sale, response.context['results'])

    def test_refund_find_filters_by_date(self):
        today = self.sale.time_of_sale.date().isoformat()
        response = self.client.get(reverse('refund_find'), {'date': today})
        self.assertTrue(response.context['any_filter'])
        self.assertIn(self.sale, response.context['results'])

        response = self.client.get(reverse('refund_find'), {'date': '2000-01-01'})
        self.assertNotIn(self.sale, response.context['results'])

    def test_refund_find_filters_by_amount(self):
        response = self.client.get(reverse('refund_find'), {'amount': '12.00'})
        self.assertIn(self.sale, response.context['results'])

        response = self.client.get(reverse('refund_find'), {'amount': '999.99'})
        self.assertNotIn(self.sale, response.context['results'])

    def test_refund_new_page_loads(self):
        response = self.client.get(reverse('refund_new', kwargs={'pk': self.sale.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['lines'][0]['remaining'], Decimal('3.000'))

    def test_partial_refund_restores_stock_and_records_refund(self):
        response = self.client.post(reverse('refund_new', kwargs={'pk': self.sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{self.item.pk}': '2',
        })

        refund = RefundAttributes.objects.get(original_sale=self.sale)
        self.assertRedirects(response, reverse('refund_details', kwargs={'pk': refund.pk}))
        self.assertEqual(refund.cashier, self.user)
        self.assertEqual(refund.reason, RefundAttributes.RETURN_COMPLAINT)
        self.assertEqual(refund.total_amount, Decimal('8.00'))

        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 9)  # 7 + 2 given back

    def test_cannot_refund_more_than_was_sold(self):
        response = self.client.post(reverse('refund_new', kwargs={'pk': self.sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{self.item.pk}': '5',
        })

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context['error'])
        self.assertFalse(RefundAttributes.objects.filter(original_sale=self.sale).exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 7)  # unchanged

    def test_second_refund_is_capped_by_what_the_first_already_took(self):
        RefundItems.objects.create(
            refund=RefundAttributes.objects.create(
                original_sale=self.sale, cashier=self.user, reason=RefundAttributes.OPERATOR_ERROR,
            ),
            original_item=self.item, refund_quantity=Decimal('2.000'), price_at_refund=Decimal('4.00'),
        )

        # Only 1.000 left refundable (3 sold - 2 already refunded) -- asking
        # for 2 more must be rejected, not silently over-refund the line.
        response = self.client.post(reverse('refund_new', kwargs={'pk': self.sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{self.item.pk}': '2',
        })

        self.assertIsNotNone(response.context['error'])
        self.assertEqual(RefundAttributes.objects.filter(original_sale=self.sale).count(), 1)

    def test_invalid_reason_is_rejected(self):
        response = self.client.post(reverse('refund_new', kwargs={'pk': self.sale.pk}), {
            'reason': 'NOT_A_REAL_REASON',
            f'refund_qty_{self.item.pk}': '1',
        })

        self.assertIsNotNone(response.context['error'])
        self.assertFalse(RefundAttributes.objects.filter(original_sale=self.sale).exists())

    def test_refund_of_recipe_product_restores_ingredient_stock(self):
        milk = Product.objects.create(
            internal_code='RF000002', name='Refund Milk', sell_price=2, quantity=Decimal('5.000'),
            unit_type=Product.WEIGHT,
        )
        cappuccino = Product.objects.create(
            internal_code='RF000003', name='Refund Cappuccino', sell_price=3, quantity=0, is_recipe=True,
        )
        RecipeIngredient.objects.create(recipe=cappuccino, ingredient=milk, quantity=Decimal('0.050'))
        sale = SaleAttributes.objects.create(cashier=self.user)
        item = SaleItems.objects.create(sale=sale, sale_item=cappuccino, sale_quantity=2)
        milk.refresh_from_db()
        self.assertEqual(milk.quantity, Decimal('4.900'))  # 5.000 - 2*0.050

        self.client.post(reverse('refund_new', kwargs={'pk': sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{item.pk}': '1',
        })

        milk.refresh_from_db()
        self.assertEqual(milk.quantity, Decimal('4.950'))  # 1 cappuccino refunded back = +0.050

    def test_refund_details_page_loads(self):
        refund = RefundAttributes.objects.create(
            original_sale=self.sale, cashier=self.user, reason=RefundAttributes.RETURN_COMPLAINT,
        )
        RefundItems.objects.create(
            refund=refund, original_item=self.item, refund_quantity=Decimal('1.000'), price_at_refund=Decimal('4.00'),
        )
        response = self.client.get(reverse('refund_details', kwargs={'pk': refund.pk}))
        self.assertEqual(response.status_code, 200)
