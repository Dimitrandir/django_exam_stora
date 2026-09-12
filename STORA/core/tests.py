from django.test import TestCase
from django.urls import reverse

from STORA.accounts.models import Employee
from STORA.products.models import Category, Product, Suppliers


class IndexPageTests(TestCase):
    def setUp(self):
        # `index` is decorated with @login_required (STORA/products/views.py)
        # -- these tests predate that and were never actually catching the
        # mismatch, because `STORA/core` was missing __init__.py and so
        # `manage.py test STORA.core` couldn't discover this file at all.
        self.user = Employee.objects.create_user(username='indexviewer', password='pass12345')
        self.client.force_login(self.user)

    def test_index_page_loads(self):
        response = self.client.get(reverse('index'))
        self.assertEqual(response.status_code, 200)

    def test_index_page_uses_template(self):
        response = self.client.get(reverse('index'))
        self.assertTemplateUsed(response, 'index.html')

    def test_index_page_redirects_when_not_logged_in(self):
        self.client.logout()
        response = self.client.get(reverse('index'))
        self.assertEqual(response.status_code, 302)


class GlobalSearchViewTests(TestCase):
    def setUp(self):
        self.user = Employee.objects.create_user(
            username='searcher', password='pass12345', role=Employee.CASHIER
        )
        self.category = Category.objects.create(name='Beverages')
        self.supplier = Suppliers.objects.create(name='Acme Trading Ltd', bulstat='123123123')
        self.product = Product.objects.create(
            internal_code='P9000001', name='Sparkling Water', sell_price=1.5, quantity=0,
            category=self.category,
        )

    def _search(self, q):
        return self.client.get(reverse('global_search'), {'q': q})

    def test_requires_login(self):
        response = self._search('water')
        self.assertIn(response.status_code, (302, 401, 403))

    def test_short_query_returns_empty_results(self):
        self.client.force_login(self.user)
        response = self._search('w')
        data = response.json()
        self.assertEqual(data, {'products': [], 'suppliers': [], 'categories': [], 'employees': []})

    def test_finds_product_by_partial_name(self):
        self.client.force_login(self.user)
        response = self._search('sparkl')
        labels = [item['label'] for item in response.json()['products']]
        self.assertTrue(any('Sparkling Water' in label for label in labels))

    def test_finds_product_by_internal_code(self):
        self.client.force_login(self.user)
        response = self._search('P9000001')
        labels = [item['label'] for item in response.json()['products']]
        self.assertTrue(any('Sparkling Water' in label for label in labels))

    def test_finds_supplier_by_name(self):
        self.client.force_login(self.user)
        response = self._search('acme')
        labels = [item['label'] for item in response.json()['suppliers']]
        self.assertIn('Acme Trading Ltd', labels)

    def test_finds_category_by_name(self):
        self.client.force_login(self.user)
        response = self._search('bever')
        labels = [item['label'] for item in response.json()['categories']]
        self.assertIn('Beverages', labels)

    def test_finds_employee_by_username(self):
        self.client.force_login(self.user)
        response = self._search('search')
        labels = [item['label'] for item in response.json()['employees']]
        self.assertTrue(any('searcher' in label for label in labels))

    def test_no_matches_returns_empty_lists_not_error(self):
        self.client.force_login(self.user)
        response = self._search('zzzznomatchzzzz')
        self.assertEqual(response.status_code, 200)

    def test_multi_word_query_matches_regardless_of_word_order(self):
        # "Sparkling Water" should turn up for either word order -- each
        # word just has to appear somewhere in the name (see
        # multi_token_icontains_q), not as one contiguous phrase.
        self.client.force_login(self.user)
        for query in ('water sparkling', 'sparkling water'):
            labels = [item['label'] for item in self._search(query).json()['products']]
            self.assertTrue(any('Sparkling Water' in label for label in labels), query)

    def test_multi_word_query_requires_every_word_present(self):
        # Every word has to match somewhere -- "sparkling" alone isn't
        # enough if a second word in the query doesn't appear at all.
        self.client.force_login(self.user)
        labels = [item['label'] for item in self._search('sparkling nomatchword').json()['products']]
        self.assertFalse(any('Sparkling Water' in label for label in labels))