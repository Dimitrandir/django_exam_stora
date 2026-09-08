from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from STORA.products.models import Product, RecipeIngredient
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
