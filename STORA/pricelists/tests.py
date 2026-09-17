from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from STORA.products.models import Category, Product, ProductSupplier, Suppliers
from STORA.pricelists.models import PriceList, PriceListRule
from STORA.pricelists.services import resolve_price, resolve_prices

User = get_user_model()


def _period(days_start=-1, days_end=1):
    today = timezone.localdate()
    return today + timedelta(days=days_start), today + timedelta(days=days_end)


class PriceListRuleValidationTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='pl_manager1', password='pass12345', role=User.MANAGER)
        self.category = Category.objects.create(name='PL Drinks')
        self.supplier = Suppliers.objects.create(name='PL Supplier Ltd', bulstat='111222333')
        self.product = Product.objects.create(
            internal_code='PL000001', name='PL Cola', sell_price=Decimal('2.00'), quantity=0,
        )
        start, end = _period()
        self.price_list = PriceList.objects.create(
            name='PL Test List', start_date=start, end_date=end, created_by=self.manager,
        )

    def test_product_rule_requires_product_field(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
                             discount_percent=Decimal('10'))
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_product_rule_rejects_category_field_set(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
                             product=self.product, category=self.category, discount_percent=Decimal('10'))
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_category_rule_rejects_fixed_price(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_CATEGORY,
                             category=self.category, fixed_price=Decimal('1.50'))
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_supplier_rule_rejects_fixed_price(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_SUPPLIER,
                             supplier=self.supplier, fixed_price=Decimal('1.50'))
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_rejects_both_discount_and_fixed_price(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
                             product=self.product, discount_percent=Decimal('10'), fixed_price=Decimal('1.50'))
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_rejects_neither_discount_nor_fixed_price(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_PRODUCT, product=self.product)
        with self.assertRaises(ValidationError):
            rule.clean()

    def test_valid_product_rule_with_percent(self):
        rule = PriceListRule(price_list=self.price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
                             product=self.product, discount_percent=Decimal('25'))
        rule.clean()  # should not raise

    def test_effective_price_percent(self):
        rule = PriceListRule(discount_percent=Decimal('25'))
        self.assertEqual(rule.effective_price(Decimal('2.00')), Decimal('1.50'))

    def test_effective_price_fixed(self):
        rule = PriceListRule(fixed_price=Decimal('1.99'))
        self.assertEqual(rule.effective_price(Decimal('2.86')), Decimal('1.99'))

    def test_end_date_before_start_date_rejected(self):
        price_list = PriceList(
            name='Backwards', created_by=self.manager,
            start_date=timezone.localdate(), end_date=timezone.localdate() - timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            price_list.clean()


class ResolvePricesTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='pl_manager2', password='pass12345', role=User.MANAGER)
        self.category = Category.objects.create(name='PL Snacks')
        self.supplier = Suppliers.objects.create(name='PL Snack Supplier', bulstat='444555666')
        self.product = Product.objects.create(
            internal_code='PL000002', name='PL Chips', sell_price=Decimal('4.00'), quantity=0,
            category=self.category,
        )
        ProductSupplier.objects.create(product=self.product, supplier=self.supplier)

    def _make_list(self, name, priority=0, scope_type=PriceListRule.SCOPE_PRODUCT, discount_percent=None,
                    fixed_price=None, days_start=-1, days_end=1, **target):
        start, end = _period(days_start, days_end)
        price_list = PriceList.objects.create(
            name=name, priority=priority, start_date=start, end_date=end, created_by=self.manager,
        )
        PriceListRule.objects.create(
            price_list=price_list, scope_type=scope_type,
            discount_percent=discount_percent, fixed_price=fixed_price, **target,
        )
        return price_list

    def test_no_matching_rule_falls_back_to_regular_price(self):
        price, rule = resolve_price(self.product)
        self.assertEqual(price, self.product.sell_price)
        self.assertIsNone(rule)

    def test_direct_product_rule_applies(self):
        self._make_list('Direct', discount_percent=Decimal('25'), product=self.product)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, Decimal('3.00'))
        self.assertIsNotNone(rule)

    def test_category_rule_applies_dynamically(self):
        self._make_list('Category Promo', discount_percent=Decimal('10'),
                        scope_type=PriceListRule.SCOPE_CATEGORY, category=self.category)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, Decimal('3.60'))

    def test_new_product_added_to_category_later_is_covered(self):
        self._make_list('Category Promo', discount_percent=Decimal('50'),
                        scope_type=PriceListRule.SCOPE_CATEGORY, category=self.category)
        new_product = Product.objects.create(
            internal_code='PL000003', name='PL New Snack', sell_price=Decimal('10.00'), quantity=0,
            category=self.category,
        )
        price, rule = resolve_price(new_product)
        self.assertEqual(price, Decimal('5.00'))

    def test_supplier_rule_applies_dynamically(self):
        self._make_list('Supplier Promo', discount_percent=Decimal('20'),
                        scope_type=PriceListRule.SCOPE_SUPPLIER, supplier=self.supplier)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, Decimal('3.20'))

    def test_expired_list_does_not_apply(self):
        self._make_list('Expired', discount_percent=Decimal('50'), product=self.product,
                        days_start=-10, days_end=-1)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, self.product.sell_price)

    def test_future_list_does_not_apply(self):
        self._make_list('Future', discount_percent=Decimal('50'), product=self.product,
                        days_start=1, days_end=10)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, self.product.sell_price)

    def test_deleted_list_does_not_apply(self):
        price_list = self._make_list('Soon Deleted', discount_percent=Decimal('50'), product=self.product)
        price_list.is_deleted = True
        price_list.save(update_fields=['is_deleted'])
        price, rule = resolve_price(self.product)
        self.assertEqual(price, self.product.sell_price)

    def test_higher_priority_list_wins(self):
        self._make_list('Low Priority', priority=1, discount_percent=Decimal('10'), product=self.product)
        high = self._make_list('High Priority', priority=10, discount_percent=Decimal('50'), product=self.product)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, Decimal('2.00'))
        self.assertEqual(rule.price_list_id, high.pk)

    def test_more_specific_scope_wins_at_equal_priority(self):
        # Same priority (default 0): a direct product rule should beat a
        # category rule that also reaches the same product.
        self._make_list('Category Level', discount_percent=Decimal('10'),
                        scope_type=PriceListRule.SCOPE_CATEGORY, category=self.category)
        self._make_list('Product Level', discount_percent=Decimal('50'), product=self.product)
        price, rule = resolve_price(self.product)
        self.assertEqual(price, Decimal('2.00'))
        self.assertEqual(rule.scope_type, PriceListRule.SCOPE_PRODUCT)

    def test_resolve_prices_batches_multiple_products(self):
        other = Product.objects.create(
            internal_code='PL000004', name='PL Other', sell_price=Decimal('6.00'), quantity=0,
        )
        self._make_list('Direct', discount_percent=Decimal('25'), product=self.product)
        matched = resolve_prices([self.product, other])
        self.assertIn(self.product.pk, matched)
        self.assertNotIn(other.pk, matched)


class PriceListViewTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='pl_manager3', password='pass12345', role=User.MANAGER)
        self.cashier = User.objects.create_user(username='pl_cashier1', password='pass12345', role=User.CASHIER)
        self.product = Product.objects.create(
            internal_code='PL000005', name='PL Juice', sell_price=Decimal('3.00'), quantity=0,
        )
        self.client.force_login(self.manager)

    def test_create_price_list_with_product_rule(self):
        start, end = _period()
        response = self.client.post(reverse('price_list_create'), {
            'name': 'Weekend Promo',
            'priority': '5',
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
            'rules_json': f'[{{"scope_type": "PRODUCT", "target_id": {self.product.pk}, "discount_percent": 20, "fixed_price": null}}]',
        })
        price_list = PriceList.objects.get(name='Weekend Promo')
        self.assertRedirects(response, reverse('price_list_detail', args=[price_list.pk]))
        self.assertEqual(price_list.rules.count(), 1)
        rule = price_list.rules.first()
        self.assertEqual(rule.product, self.product)
        self.assertEqual(rule.discount_percent, Decimal('20'))

    def test_create_rejects_empty_rules(self):
        start, end = _period()
        response = self.client.post(reverse('price_list_create'), {
            'name': 'Empty List', 'priority': '0',
            'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'rules_json': '[]',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PriceList.objects.filter(name='Empty List').exists())

    def test_create_rejects_both_discount_and_fixed_price_in_one_row(self):
        start, end = _period()
        response = self.client.post(reverse('price_list_create'), {
            'name': 'Bad Row List', 'priority': '0',
            'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'rules_json': f'[{{"scope_type": "PRODUCT", "target_id": {self.product.pk}, "discount_percent": 10, "fixed_price": 1.5}}]',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PriceList.objects.filter(name='Bad Row List').exists())

    def test_cashier_cannot_create_price_list(self):
        self.client.force_login(self.cashier)
        start, end = _period()
        response = self.client.post(reverse('price_list_create'), {
            'name': 'Nope', 'priority': '0',
            'start_date': start.isoformat(), 'end_date': end.isoformat(), 'rules_json': '[]',
        })
        self.assertEqual(response.status_code, 403)

    def test_delete_soft_deletes(self):
        start, end = _period()
        price_list = PriceList.objects.create(name='To Delete', start_date=start, end_date=end, created_by=self.manager)
        response = self.client.post(reverse('price_list_delete', args=[price_list.pk]))
        self.assertRedirects(response, reverse('price_list_list'))
        price_list.refresh_from_db()
        self.assertTrue(price_list.is_deleted)

    def test_deleted_lists_hidden_from_list_by_default(self):
        start, end = _period()
        price_list = PriceList.objects.create(
            name='Hidden List', start_date=start, end_date=end, created_by=self.manager, is_deleted=True,
        )
        response = self.client.get(reverse('price_list_list'))
        self.assertNotIn(price_list, response.context['price_lists'])

    def test_deleted_lists_shown_with_show_deleted_param(self):
        start, end = _period()
        price_list = PriceList.objects.create(
            name='Hidden List 2', start_date=start, end_date=end, created_by=self.manager, is_deleted=True,
        )
        response = self.client.get(reverse('price_list_list'), {'show_deleted': '1'})
        self.assertIn(price_list, response.context['price_lists'])

    def test_check_conflict_finds_overlapping_list(self):
        start, end = _period()
        existing = PriceList.objects.create(name='Existing', start_date=start, end_date=end, created_by=self.manager)
        PriceListRule.objects.create(
            price_list=existing, scope_type=PriceListRule.SCOPE_PRODUCT,
            product=self.product, discount_percent=Decimal('15'),
        )
        response = self.client.get(reverse('price_list_check_conflict'), {
            'scope_type': 'PRODUCT', 'target_id': self.product.pk,
        })
        data = response.json()
        self.assertEqual(len(data['conflicts']), 1)
        self.assertEqual(data['conflicts'][0]['price_list_name'], 'Existing')

    def test_check_conflict_excludes_given_price_list(self):
        start, end = _period()
        existing = PriceList.objects.create(name='Self', start_date=start, end_date=end, created_by=self.manager)
        PriceListRule.objects.create(
            price_list=existing, scope_type=PriceListRule.SCOPE_PRODUCT,
            product=self.product, discount_percent=Decimal('15'),
        )
        response = self.client.get(reverse('price_list_check_conflict'), {
            'scope_type': 'PRODUCT', 'target_id': self.product.pk, 'exclude_pk': existing.pk,
        })
        data = response.json()
        self.assertEqual(len(data['conflicts']), 0)

    def test_detail_view_shows_covered_product(self):
        start, end = _period()
        price_list = PriceList.objects.create(name='Detail Test', start_date=start, end_date=end, created_by=self.manager)
        PriceListRule.objects.create(
            price_list=price_list, scope_type=PriceListRule.SCOPE_PRODUCT,
            product=self.product, discount_percent=Decimal('10'),
        )
        response = self.client.get(reverse('price_list_detail', args=[price_list.pk]))
        self.assertEqual(response.status_code, 200)
        items = response.context['items_data']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['new_price'], 2.7)
