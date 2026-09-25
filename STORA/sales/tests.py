import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from django.utils import timezone

from STORA.products.models import Category, Product, RecipeIngredient, TaxGroup
from STORA.pricelists.models import PriceList, PriceListRule
from STORA.sales import fiscal
from STORA.sales.models import SaleAttributes, SaleItems, PosPin, SaleItemVoidLog, RefundAttributes, RefundItems, FiscalCounter
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


FISCAL_SETTINGS = dict(
    FISCAL_ENABLED=True,
    FISCAL_ECRCOMMAPP_PATH='/fake/ecrcommapp.exe',
    FISCAL_COM_PORT='COM4',
    FISCAL_API_URL='http://127.0.0.1:7000/Api',
    FISCAL_DEVICE_SERIAL='DY000001',
    FISCAL_OPERATOR_NUM='1',
    FISCAL_OPERATOR_PASSWORD='1',
)


class FiscalCounterTests(TestCase):
    def test_next_increments_and_is_unique(self):
        first = FiscalCounter.next()
        second = FiscalCounter.next()
        self.assertEqual(second, first + 1)


class FiscalHelperTests(TestCase):
    def setUp(self):
        self.tax_group = TaxGroup.objects.create(name='Standard', rate=Decimal('20.00'), fiscal_letter='Б')
        self.product = Product.objects.create(
            internal_code='P0000020', name='Taxed Product', sell_price=Decimal('5.00'), quantity=Decimal('10.000'),
            tax_group=self.tax_group,
        )

    def test_tax_letter_for_returns_configured_letter(self):
        self.assertEqual(fiscal._tax_letter_for(self.product), 'Б')

    def test_tax_letter_for_raises_when_no_tax_group(self):
        product = Product.objects.create(
            internal_code='P0000021', name='No Group', sell_price=Decimal('1.00'), quantity=Decimal('1.000'),
        )
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal._tax_letter_for(product)

    def test_tax_letter_for_raises_when_letter_blank(self):
        blank_group = TaxGroup.objects.create(name='Unmapped', rate=Decimal('9.00'))
        product = Product.objects.create(
            internal_code='P0000022', name='Blank Letter', sell_price=Decimal('1.00'), quantity=Decimal('1.000'),
            tax_group=blank_group,
        )
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal._tax_letter_for(product)

    @override_settings(**FISCAL_SETTINGS)
    def test_next_unic_sale_num_format(self):
        num = fiscal._next_unic_sale_num()
        self.assertRegex(num, r'^DY000001-OP01-\d{7}$')


class FiscalPrintReceiptTests(TestCase):
    """print_fiscal_receipt orchestration -- _post_command/_start_ecrcommapp/
    _stop_ecrcommapp are mocked out (no real subprocess/HTTP), so this only
    verifies the command sequence/cleanup logic, not the real device
    protocol (that was verified live, see kasov_aparat.md)."""

    def setUp(self):
        self.tax_group = TaxGroup.objects.create(name='Standard', rate=Decimal('20.00'), fiscal_letter='Б')
        self.product = Product.objects.create(
            internal_code='P0000030', name='Fiscal Product', sell_price=Decimal('5.00'), quantity=Decimal('10.000'),
            tax_group=self.tax_group,
        )
        self.cashier = User.objects.create_user(username='fiscal-cashier', password='pass12345')
        self.sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH, amount_paid=Decimal('5.00'),
        )
        SaleItems.objects.create(
            sale=self.sale, sale_item=self.product, sale_quantity=Decimal('1.000'), price_at_sale=Decimal('5.00'),
        )

    def test_disabled_raises_without_touching_subprocess(self):
        with patch('STORA.sales.fiscal._start_ecrcommapp') as mock_start:
            with self.assertRaises(fiscal.FiscalPrintError):
                fiscal.print_fiscal_receipt(self.sale)
            mock_start.assert_not_called()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_happy_path_sends_expected_command_sequence(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        mock_post.return_value = {'FiscReceipt': 3, 'AllReceipt': 5}

        result = fiscal.print_fiscal_receipt(self.sale)

        sent_commands = [call.args[0] for call in mock_post.call_args_list]
        self.assertEqual(
            sent_commands, ['FDStartFiscRcp', 'FDSaleItem', 'FDPrintBarcode', 'FDTotalSum', 'FDEndFiscRcp'],
        )
        self.assertTrue(result['unic_sale_num'].startswith('DY000001-OP01-'))
        self.assertEqual(result['receipt_number'], 3)
        mock_stop.assert_called_once()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_barcode_encodes_the_sale_pk(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        mock_post.return_value = {}

        fiscal.print_fiscal_receipt(self.sale)

        barcode_call = next(call for call in mock_post.call_args_list if call.args[0] == 'FDPrintBarcode')
        self.assertEqual(barcode_call.args[1]['Data'], str(self.sale.pk))
        self.assertEqual(barcode_call.args[1]['Type'], 'Code128')

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_failure_after_open_attempts_cancel(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()

        def side_effect(cmd, cmd_data, timeout=15):
            if cmd == 'FDSaleItem':
                raise fiscal.FiscalPrintError('boom')
            return {}

        mock_post.side_effect = side_effect

        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_fiscal_receipt(self.sale)

        sent_commands = [call.args[0] for call in mock_post.call_args_list]
        self.assertEqual(sent_commands, ['FDStartFiscRcp', 'FDSaleItem', 'FDCancelRcp'])
        mock_stop.assert_called_once()

    @override_settings(**FISCAL_SETTINGS)
    def test_missing_tax_group_letter_raises_before_opening_receipt(self):
        untaxed_product = Product.objects.create(
            internal_code='P0000031', name='No Tax Letter', sell_price=Decimal('3.00'), quantity=Decimal('5.000'),
        )
        sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH, amount_paid=Decimal('3.00'),
        )
        SaleItems.objects.create(
            sale=sale, sale_item=untaxed_product, sale_quantity=Decimal('1.000'), price_at_sale=Decimal('3.00'),
        )

        with patch('STORA.sales.fiscal._start_ecrcommapp') as mock_start:
            with self.assertRaises(fiscal.FiscalPrintError):
                fiscal.print_fiscal_receipt(sale)
            mock_start.assert_not_called()


class FiscalAttemptPrintTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username='attempt-cashier', password='pass12345')
        self.sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH, amount_paid=Decimal('1.00'),
        )

    @patch('STORA.sales.fiscal.print_fiscal_receipt')
    def test_success_marks_printed(self, mock_print):
        mock_print.return_value = {'unic_sale_num': 'DY000001-OP01-0000001', 'receipt_number': 3}
        fiscal.attempt_print(self.sale)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.fiscal_status, SaleAttributes.FISCAL_PRINTED)
        self.assertEqual(self.sale.fiscal_unic_sale_num, 'DY000001-OP01-0000001')
        self.assertEqual(self.sale.fiscal_receipt_number, 3)
        self.assertIsNotNone(self.sale.fiscal_printed_at)

    @patch('STORA.sales.fiscal.print_fiscal_receipt')
    def test_failure_marks_failed_and_does_not_raise(self, mock_print):
        mock_print.side_effect = fiscal.FiscalPrintError('device unreachable')
        fiscal.attempt_print(self.sale)  # must not raise
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.fiscal_status, SaleAttributes.FISCAL_FAILED)
        self.assertIn('device unreachable', self.sale.fiscal_error)


class SalesAddFiscalIntegrationTests(TestCase):
    """sales_add view's decision of WHETHER to call fiscal.attempt_print --
    the call itself is mocked, so this doesn't touch real hardware."""

    def setUp(self):
        self.cashier = User.objects.create_user(username='fiscal-integ-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.product = Product.objects.create(
            internal_code='P0000040', name='Integration Product', sell_price=Decimal('2.00'), quantity=Decimal('10.000'),
        )

    def _post_data(self, payment_method, **overrides):
        data = {
            'cashier': self.cashier.pk,
            'payment_method': payment_method,
            'amount_paid': '2.00',
            'change_due': '0.00',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-sale_item': self.product.pk,
            'items-0-sale_quantity': '1',
            'items-0-price_at_sale': '2.00',
            'items-0-total_price_row': '2.00',
            'items-0-DELETE': '',
        }
        data.update(overrides)
        return data

    @override_settings(FISCAL_ENABLED=True)
    @patch('STORA.sales.views.fiscal.attempt_print')
    def test_cash_sale_triggers_attempt_print_when_enabled(self, mock_attempt):
        self.client.post(reverse('sale_add'), self._post_data(SaleAttributes.CASH))
        mock_attempt.assert_called_once()

    @override_settings(FISCAL_ENABLED=True)
    @patch('STORA.sales.views.fiscal.attempt_print')
    def test_card_sale_never_triggers_attempt_print(self, mock_attempt):
        data = self._post_data(SaleAttributes.CARD, amount_paid='', card_amount='2.00')
        self.client.post(reverse('sale_add'), data)
        mock_attempt.assert_not_called()

    @override_settings(FISCAL_ENABLED=False)
    @patch('STORA.sales.views.fiscal.attempt_print')
    def test_disabled_never_triggers_attempt_print(self, mock_attempt):
        self.client.post(reverse('sale_add'), self._post_data(SaleAttributes.CASH))
        mock_attempt.assert_not_called()


class SaleFiscalRetryViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='fiscal-retry-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.sale = SaleAttributes.objects.create(
            cashier=self.manager, payment_method=SaleAttributes.CASH, amount_paid=Decimal('1.00'),
            fiscal_status=SaleAttributes.FISCAL_FAILED, fiscal_error='device unreachable',
        )

    @override_settings(FISCAL_ENABLED=True)
    @patch('STORA.sales.views.fiscal.attempt_print')
    def test_retry_calls_attempt_print_and_redirects(self, mock_attempt):
        response = self.client.post(reverse('sale_fiscal_retry', kwargs={'pk': self.sale.pk}))
        mock_attempt.assert_called_once()
        self.assertRedirects(response, reverse('sale_details', kwargs={'pk': self.sale.pk}))

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(reverse('sale_fiscal_retry', kwargs={'pk': self.sale.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)


class FiscalDailyReportTests(TestCase):
    """print_x_report/print_z_report -- _post_command/_start_ecrcommapp/
    _stop_ecrcommapp mocked, so this checks the Item value/orchestration,
    not the real device."""

    def test_disabled_raises(self):
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_x_report()
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_z_report()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_x_report_uses_item_1_and_stops_app(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        fiscal.print_x_report()
        mock_post.assert_called_once_with('FDDailyRpt', {'Item': 1, 'Option': ''})
        mock_stop.assert_called_once()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_z_report_uses_item_0_and_stops_app(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        fiscal.print_z_report()
        mock_post.assert_called_once_with('FDDailyRpt', {'Item': 0, 'Option': ''})
        mock_stop.assert_called_once()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_app_is_stopped_even_if_report_fails(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        mock_post.side_effect = fiscal.FiscalPrintError('boom')
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_x_report()
        mock_stop.assert_called_once()


class FiscalPeriodReportTests(TestCase):
    def test_disabled_raises(self):
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_period_report(timezone.now().date(), timezone.now().date())

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_dates_formatted_as_ddmmyy(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        start = timezone.datetime(2026, 2, 1).date()
        end = timezone.datetime(2026, 3, 14).date()

        fiscal.print_period_report(start, end)

        mock_post.assert_called_once_with('FDRptFromFMByDate', {
            'StartDate': '010226', 'EndDate': '140326', 'PAY': 'PAY',
        })


class FiscalReportsViewTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username='fiscal-rpt-cashier', password='pass12345', role=User.CASHIER)
        self.warehouse = User.objects.create_user(username='fiscal-rpt-warehouse', password='pass12345', role=User.WAREHOUSE)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('fiscal_reports'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_warehouse_has_no_access(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('fiscal_reports')).status_code, 403)

    def test_cashier_can_view_page(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse('fiscal_reports')).status_code, 200)

    @patch('STORA.sales.views.fiscal.print_x_report')
    def test_x_report_success(self, mock_print):
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('fiscal_report_x'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        mock_print.assert_called_once()

    @patch('STORA.sales.views.fiscal.print_z_report')
    def test_z_report_failure_returns_error_json(self, mock_print):
        mock_print.side_effect = fiscal.FiscalPrintError('device unreachable')
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('fiscal_report_z'))
        self.assertEqual(response.status_code, 502)
        self.assertIn('device unreachable', response.json()['error'])

    @patch('STORA.sales.views.fiscal.print_period_report')
    def test_period_report_success(self, mock_print):
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('fiscal_report_period'), {
            'start_date': '2026-02-01', 'end_date': '2026-03-14',
        })
        self.assertEqual(response.status_code, 200)
        mock_print.assert_called_once()

    def test_period_report_missing_dates_returns_400(self):
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('fiscal_report_period'), {})
        self.assertEqual(response.status_code, 400)

    def test_period_report_end_before_start_returns_400(self):
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('fiscal_report_period'), {
            'start_date': '2026-03-14', 'end_date': '2026-02-01',
        })
        self.assertEqual(response.status_code, 400)


class FiscalRefundTests(TestCase):
    """print_fiscal_refund orchestration -- same mocked boundary as
    FiscalPrintReceiptTests."""

    def setUp(self):
        self.tax_group = TaxGroup.objects.create(name='Standard', rate=Decimal('20.00'), fiscal_letter='Б')
        self.product = Product.objects.create(
            internal_code='P0000050', name='Refundable Product', sell_price=Decimal('5.00'),
            quantity=Decimal('10.000'), tax_group=self.tax_group,
        )
        self.cashier = User.objects.create_user(username='refund-fiscal-cashier', password='pass12345')
        self.sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH, amount_paid=Decimal('5.00'),
            fiscal_status=SaleAttributes.FISCAL_PRINTED, fiscal_receipt_number=3,
            fiscal_printed_at=timezone.datetime(2026, 3, 14, 12, 30, tzinfo=timezone.get_current_timezone()),
        )
        self.sale_item = SaleItems.objects.create(
            sale=self.sale, sale_item=self.product, sale_quantity=Decimal('1.000'), price_at_sale=Decimal('5.00'),
        )
        self.refund = RefundAttributes.objects.create(
            original_sale=self.sale, cashier=self.cashier, reason=RefundAttributes.RETURN_COMPLAINT,
        )
        self.refund_item = RefundItems.objects.create(
            refund=self.refund, original_item=self.sale_item, refund_quantity=Decimal('1.000'),
            price_at_refund=Decimal('5.00'),
        )

    def test_disabled_raises(self):
        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_fiscal_refund(self.refund)

    @override_settings(**FISCAL_SETTINGS)
    def test_original_sale_not_fiscalized_raises_without_touching_subprocess(self):
        self.sale.fiscal_status = SaleAttributes.FISCAL_NONE
        self.sale.fiscal_receipt_number = None
        self.sale.save()

        with patch('STORA.sales.fiscal._start_ecrcommapp') as mock_start:
            with self.assertRaises(fiscal.FiscalPrintError):
                fiscal.print_fiscal_refund(self.refund)
            mock_start.assert_not_called()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_happy_path_sends_refund_fields(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()
        mock_post.return_value = {}

        unic_sale_num = fiscal.print_fiscal_refund(self.refund)

        sent_commands = [call.args[0] for call in mock_post.call_args_list]
        self.assertEqual(sent_commands, ['FDStartFiscRcp', 'FDSaleItem', 'FDTotalSum', 'FDEndFiscRcp'])

        start_call = mock_post.call_args_list[0]
        start_data = start_call.args[1]
        self.assertEqual(start_data['Refund'], 'R')
        self.assertEqual(start_data['Reason'], 0)  # RETURN_COMPLAINT -> 0
        self.assertEqual(start_data['DocLink'], 3)
        self.assertEqual(start_data['DocLinkDT'], '14-03-26 12:30')

        item_call = mock_post.call_args_list[1]
        item_data = item_call.args[1]
        self.assertEqual(item_data['Sale type'], 'Refund')

        self.assertTrue(unic_sale_num.startswith('DY000001-OP01-'))
        mock_stop.assert_called_once()

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_operator_error_reason_maps_to_1(self, mock_stop, mock_start, mock_post):
        self.refund.reason = RefundAttributes.OPERATOR_ERROR
        self.refund.save()
        mock_start.return_value = MagicMock()
        mock_post.return_value = {}

        fiscal.print_fiscal_refund(self.refund)

        start_data = mock_post.call_args_list[0].args[1]
        self.assertEqual(start_data['Reason'], 1)

    @override_settings(**FISCAL_SETTINGS)
    @patch('STORA.sales.fiscal._post_command')
    @patch('STORA.sales.fiscal._start_ecrcommapp')
    @patch('STORA.sales.fiscal._stop_ecrcommapp')
    def test_failure_after_open_attempts_cancel(self, mock_stop, mock_start, mock_post):
        mock_start.return_value = MagicMock()

        def side_effect(cmd, cmd_data, timeout=15):
            if cmd == 'FDSaleItem':
                raise fiscal.FiscalPrintError('boom')
            return {}

        mock_post.side_effect = side_effect

        with self.assertRaises(fiscal.FiscalPrintError):
            fiscal.print_fiscal_refund(self.refund)

        sent_commands = [call.args[0] for call in mock_post.call_args_list]
        self.assertEqual(sent_commands, ['FDStartFiscRcp', 'FDSaleItem', 'FDCancelRcp'])


class FiscalAttemptPrintRefundTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username='refund-attempt-cashier', password='pass12345')
        self.sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH, amount_paid=Decimal('1.00'),
            fiscal_status=SaleAttributes.FISCAL_PRINTED, fiscal_receipt_number=1, fiscal_printed_at=timezone.now(),
        )
        self.refund = RefundAttributes.objects.create(
            original_sale=self.sale, cashier=self.cashier, reason=RefundAttributes.RETURN_COMPLAINT,
        )

    @patch('STORA.sales.fiscal.print_fiscal_refund')
    def test_success_marks_printed(self, mock_print):
        mock_print.return_value = 'DY000001-OP01-0000002'
        fiscal.attempt_print_refund(self.refund)
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.fiscal_status, SaleAttributes.FISCAL_PRINTED)
        self.assertEqual(self.refund.fiscal_unic_sale_num, 'DY000001-OP01-0000002')
        self.assertIsNotNone(self.refund.fiscal_printed_at)

    @patch('STORA.sales.fiscal.print_fiscal_refund')
    def test_failure_marks_failed_and_does_not_raise(self, mock_print):
        mock_print.side_effect = fiscal.FiscalPrintError('device unreachable')
        fiscal.attempt_print_refund(self.refund)  # must not raise
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.fiscal_status, SaleAttributes.FISCAL_FAILED)
        self.assertIn('device unreachable', self.refund.fiscal_error)


class RefundNewViewFiscalIntegrationTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user(username='refund-view-cashier', password='pass12345', role=User.CASHIER)
        self.client.force_login(self.cashier)
        self.product = Product.objects.create(
            internal_code='P0000051', name='Refund View Product', sell_price=Decimal('3.00'), quantity=Decimal('10.000'),
        )

    def _make_sale(self, payment_method):
        sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=payment_method,
            amount_paid=Decimal('3.00') if payment_method == SaleAttributes.CASH else None,
            card_amount=Decimal('3.00') if payment_method == SaleAttributes.CARD else None,
            fiscal_status=SaleAttributes.FISCAL_PRINTED, fiscal_receipt_number=1, fiscal_printed_at=timezone.now(),
        )
        item = SaleItems.objects.create(
            sale=sale, sale_item=self.product, sale_quantity=Decimal('1.000'), price_at_sale=Decimal('3.00'),
        )
        return sale, item

    @override_settings(FISCAL_ENABLED=True)
    @patch('STORA.sales.views.fiscal.attempt_print_refund')
    def test_cash_original_sale_triggers_attempt_print_refund(self, mock_attempt):
        sale, item = self._make_sale(SaleAttributes.CASH)
        self.client.post(reverse('refund_new', kwargs={'pk': sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{item.pk}': '1',
        })
        mock_attempt.assert_called_once()

    @override_settings(FISCAL_ENABLED=True)
    @patch('STORA.sales.views.fiscal.attempt_print_refund')
    def test_card_original_sale_never_triggers_attempt_print_refund(self, mock_attempt):
        sale, item = self._make_sale(SaleAttributes.CARD)
        self.client.post(reverse('refund_new', kwargs={'pk': sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{item.pk}': '1',
        })
        mock_attempt.assert_not_called()

    @override_settings(FISCAL_ENABLED=False)
    @patch('STORA.sales.views.fiscal.attempt_print_refund')
    def test_disabled_never_triggers_attempt_print_refund(self, mock_attempt):
        sale, item = self._make_sale(SaleAttributes.CASH)
        self.client.post(reverse('refund_new', kwargs={'pk': sale.pk}), {
            'reason': RefundAttributes.RETURN_COMPLAINT,
            f'refund_qty_{item.pk}': '1',
        })
        mock_attempt.assert_not_called()


class RefundFiscalRetryViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='refund-retry-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)
        self.sale = SaleAttributes.objects.create(
            cashier=self.manager, payment_method=SaleAttributes.CASH, amount_paid=Decimal('1.00'),
            fiscal_status=SaleAttributes.FISCAL_PRINTED, fiscal_receipt_number=1, fiscal_printed_at=timezone.now(),
        )
        self.refund = RefundAttributes.objects.create(
            original_sale=self.sale, cashier=self.manager, reason=RefundAttributes.RETURN_COMPLAINT,
            fiscal_status=SaleAttributes.FISCAL_FAILED, fiscal_error='device unreachable',
        )

    @override_settings(FISCAL_ENABLED=True)
    @patch('STORA.sales.views.fiscal.attempt_print_refund')
    def test_retry_calls_attempt_print_refund_and_redirects(self, mock_attempt):
        response = self.client.post(reverse('refund_fiscal_retry', kwargs={'pk': self.refund.pk}))
        mock_attempt.assert_called_once()
        self.assertRedirects(response, reverse('refund_details', kwargs={'pk': self.refund.pk}))

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(reverse('refund_fiscal_retry', kwargs={'pk': self.refund.pk}))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)
