from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from STORA.products.models import Category, Product, RecipeIngredient
from STORA.sales.models import SaleAttributes, SaleItems
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

    def test_sales_list_page_loads(self):
        response = self.client.get(reverse('sales_list'))
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
        for url in [reverse('sale_add'), reverse('sales_list'), reverse('sale_details', args=[self.sale.pk]),
                    reverse('sale_delete', args=[self.sale.pk])]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn('/accounts/login/', response.url, url)

    def test_warehouse_has_no_sales_access(self):
        self.client.force_login(self.warehouse)
        self.assertEqual(self.client.get(reverse('sale_add')).status_code, 403)
        self.assertEqual(self.client.get(reverse('sales_list')).status_code, 403)
        self.assertEqual(self.client.get(reverse('sale_details', args=[self.sale.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('sale_delete', args=[self.sale.pk])).status_code, 403)

    def test_cashier_has_full_sales_access(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get(reverse('sale_add')).status_code, 200)
        self.assertEqual(self.client.get(reverse('sales_list')).status_code, 200)
        self.assertEqual(self.client.get(reverse('sale_details', args=[self.sale.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('sale_delete', args=[self.sale.pk])).status_code, 200)

    def test_manager_has_full_sales_access(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('sale_add')).status_code, 200)
        self.assertEqual(self.client.get(reverse('sales_list')).status_code, 200)


class SalesListDisplayTests(TestCase):
    """sales_list.html had a malformed {% for %}/{% empty %} (a dangling
    <tr> after every real row, and the 'no sales' message never rendering
    at all) -- same shape of bug fixed earlier for deliveries_list.html."""

    def setUp(self):
        self.manager = User.objects.create_user(username='list-manager', password='pass12345', role=User.MANAGER)
        self.client.force_login(self.manager)

    def test_empty_state_message_renders(self):
        response = self.client.get(reverse('sales_list'))
        self.assertContains(response, 'No sales found.')

    def test_real_row_does_not_duplicate_into_a_stray_empty_row(self):
        sale = SaleAttributes.objects.create(cashier=self.manager)
        response = self.client.get(reverse('sales_list'))
        self.assertContains(response, str(sale.id))
        self.assertNotContains(response, 'No sales found.')


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
        self.assertRedirects(response, reverse('sales_list'))

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
        self.assertRedirects(response, reverse('sales_list'))

        sale = SaleAttributes.objects.get(cashier=self.cashier)
        self.assertEqual(sale.payment_method, SaleAttributes.CASH)
        self.assertEqual(sale.amount_paid, Decimal('20.00'))
        self.assertEqual(sale.change_due, Decimal('10.00'))


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
        self.assertIsNone(sale.change_due)

    def test_stores_cash_payment_with_change(self):
        sale = SaleAttributes.objects.create(
            cashier=self.cashier, payment_method=SaleAttributes.CASH,
            amount_paid=Decimal('10.00'), change_due=Decimal('2.50'),
        )
        self.assertEqual(sale.payment_method, SaleAttributes.CASH)
        self.assertEqual(sale.change_due, Decimal('2.50'))


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
