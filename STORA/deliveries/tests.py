from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, Suppliers
from STORA.products.models import Product


User = get_user_model()


class DeliveryViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='receiver',
            password='pass12345',
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

        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user,
            supplier=self.supplier,
            document_type='Invoice',
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
        self.delivery = DeliveryAttributes.objects.create(
            receiver=self.user, supplier=self.supplier, document_type='Invoice',
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