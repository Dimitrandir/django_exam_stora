from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from STORA.products.models import Product
from STORA.revisions.models import RevisionAttributes, RevisionItems

User = get_user_model()


class RevisionModelTests(TestCase):
    def setUp(self):
        self.warehouse = User.objects.create_user(username='rev_wh1', password='pass12345', role=User.WAREHOUSE)
        self.product = Product.objects.create(
            internal_code='REV0001', name='Canned Beans', sell_price=Decimal('2.00'),
            delivery_price=Decimal('1.20'), quantity=Decimal('10.000'),
        )

    def test_only_one_open_revision_allowed(self):
        RevisionAttributes.objects.create(started_by=self.warehouse)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RevisionAttributes.objects.create(started_by=self.warehouse)

    def test_second_open_revision_allowed_after_first_completes(self):
        first = RevisionAttributes.objects.create(started_by=self.warehouse)
        first.status = RevisionAttributes.STATUS_COMPLETED
        first.save(update_fields=['status'])
        # Should not raise -- the partial unique index only restricts rows
        # actually marked OPEN.
        RevisionAttributes.objects.create(started_by=self.warehouse)

    def test_add_count_creates_row_with_snapshotted_system_quantity_and_price(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        item = RevisionItems.objects.add_count(revision, self.product, Decimal('3'))
        self.assertEqual(item.found_quantity, Decimal('3'))
        self.assertEqual(item.system_quantity_at_start, Decimal('10.000'))
        self.assertEqual(item.price_at_revision, Decimal('1.20'))

    def test_add_count_accumulates_instead_of_overwriting(self):
        # Simulates two separate scans (could be from two different
        # computers) of the same product within the same revision.
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('3'))
        RevisionItems.objects.add_count(revision, self.product, Decimal('4'))
        item = RevisionItems.objects.get(revision=revision, product=self.product)
        self.assertEqual(item.found_quantity, Decimal('7'))

    def test_add_count_does_not_move_system_quantity_snapshot_on_second_call(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('1'))
        # A sale happens elsewhere, mid-revision.
        self.product.quantity = Decimal('9.000')
        self.product.save(update_fields=['quantity'])
        item = RevisionItems.objects.add_count(revision, self.product, Decimal('1'))
        self.assertEqual(item.system_quantity_at_start, Decimal('10.000'))


class RevisionViewTests(TestCase):
    def setUp(self):
        self.warehouse = User.objects.create_user(username='rev_wh2', password='pass12345', role=User.WAREHOUSE)
        self.cashier = User.objects.create_user(username='rev_cash1', password='pass12345', role=User.CASHIER)
        self.product = Product.objects.create(
            internal_code='REV0002', name='Bag of Rice', sell_price=Decimal('3.00'),
            delivery_price=Decimal('1.80'), quantity=Decimal('5.000'),
        )
        self.client.force_login(self.warehouse)

    def test_start_creates_open_revision(self):
        response = self.client.post(reverse('revision_start'))
        revision = RevisionAttributes.objects.get()
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        self.assertEqual(revision.status, RevisionAttributes.STATUS_OPEN)
        self.assertEqual(revision.started_by, self.warehouse)

    def test_start_when_one_already_open_joins_it_instead_of_erroring(self):
        existing = RevisionAttributes.objects.create(started_by=self.warehouse)
        response = self.client.post(reverse('revision_start'))
        self.assertRedirects(response, reverse('revision_view', args=[existing.pk]))
        self.assertEqual(RevisionAttributes.objects.count(), 1)

    def test_cashier_cannot_start_a_revision(self):
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('revision_start'))
        self.assertEqual(response.status_code, 403)

    def test_add_item_endpoint_accumulates_across_requests(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        # Two separate requests -- stands in for two different computers
        # both counting the same product.
        self.client.post(reverse('revision_add_item', args=[revision.pk]),
                          {'product_id': self.product.pk, 'quantity': '2'})
        response = self.client.post(reverse('revision_add_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '5'})
        data = response.json()
        self.assertEqual(data['found_quantity'], 7)
        self.assertEqual(data['system_quantity_at_start'], 5)
        self.assertEqual(data['found_sum'], 7 * 1.8)
        self.assertEqual(data['quantity_diff'], 2)
        self.assertEqual(data['sum_diff'], round(7 * 1.8 - 5 * 1.8, 2))

    def test_add_item_rejected_once_revision_is_completed(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.save(update_fields=['status'])
        response = self.client.post(reverse('revision_add_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '1'})
        self.assertEqual(response.status_code, 409)

    def test_remove_item_deletes_the_row(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        item = RevisionItems.objects.add_count(revision, self.product, Decimal('3'))
        response = self.client.post(reverse('revision_remove_item', args=[revision.pk, item.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RevisionItems.objects.filter(pk=item.pk).exists())

    def test_remove_item_does_not_touch_product_stock(self):
        # Removing a miscounted row from the revision is just "forget this
        # count happened" -- stock is only ever touched at Complete time,
        # so there's nothing to undo here.
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        item = RevisionItems.objects.add_count(revision, self.product, Decimal('3'))
        self.client.post(reverse('revision_remove_item', args=[revision.pk, item.pk]))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('5.000'))

    def test_remove_item_rejected_once_revision_is_completed(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        item = RevisionItems.objects.add_count(revision, self.product, Decimal('3'))
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.save(update_fields=['status'])
        response = self.client.post(reverse('revision_remove_item', args=[revision.pk, item.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(RevisionItems.objects.filter(pk=item.pk).exists())

    def test_remove_item_unknown_id_returns_404(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        response = self.client.post(reverse('revision_remove_item', args=[revision.pk, 999999]))
        self.assertEqual(response.status_code, 404)

    def test_cashier_cannot_remove_item(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        item = RevisionItems.objects.add_count(revision, self.product, Decimal('3'))
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('revision_remove_item', args=[revision.pk, item.pk]))
        self.assertEqual(response.status_code, 403)

    def test_complete_corrects_product_quantity_to_found_amount(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('8'))
        response = self.client.post(reverse('revision_complete', args=[revision.pk]))
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('8'))
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionAttributes.STATUS_COMPLETED)
        self.assertEqual(revision.completed_by, self.warehouse)

    def test_complete_leaves_matching_products_untouched(self):
        # found == system already -- nothing to correct, but must not error.
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('5'))
        self.client.post(reverse('revision_complete', args=[revision.pk]))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('5.000'))

    def test_cancel_discards_revision_without_touching_stock(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('8'))
        response = self.client.post(reverse('revision_cancel', args=[revision.pk]))
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, Decimal('5.000'))
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionAttributes.STATUS_CANCELLED)
        self.assertEqual(revision.completed_by, self.warehouse)
        self.assertIsNotNone(revision.completed_at)

    def test_cancel_keeps_the_counted_rows(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('8'))
        self.client.post(reverse('revision_cancel', args=[revision.pk]))
        self.assertEqual(RevisionItems.objects.filter(revision=revision).count(), 1)

    def test_cancel_frees_the_slot_for_a_new_revision(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        self.client.post(reverse('revision_cancel', args=[revision.pk]))
        # Should not raise -- the partial unique index only restricts rows
        # actually marked OPEN, and cancel moved this one off OPEN.
        RevisionAttributes.objects.create(started_by=self.warehouse)

    def test_cancelled_revision_rejects_further_cancellation(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_CANCELLED
        revision.save(update_fields=['status'])
        response = self.client.post(reverse('revision_cancel', args=[revision.pk]))
        self.assertEqual(response.status_code, 404)

    def test_cashier_cannot_cancel(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        self.client.force_login(self.cashier)
        response = self.client.post(reverse('revision_cancel', args=[revision.pk]))
        self.assertEqual(response.status_code, 403)

    def test_cancelled_revision_renders_readonly_template(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_CANCELLED
        revision.save(update_fields=['status'])
        response = self.client.get(reverse('revision_view', args=[revision.pk]))
        self.assertTemplateUsed(response, 'revisions/revision_details.html')
        self.assertContains(response, 'Cancelled by')

    def test_completed_revision_rejects_further_completion(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.save(update_fields=['status'])
        response = self.client.post(reverse('revision_complete', args=[revision.pk]))
        self.assertEqual(response.status_code, 404)

    def test_open_revision_renders_live_detail_template(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        response = self.client.get(reverse('revision_view', args=[revision.pk]))
        self.assertTemplateUsed(response, 'revisions/revision_detail.html')

    def test_completed_revision_renders_readonly_template(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.save(update_fields=['status'])
        response = self.client.get(reverse('revision_view', args=[revision.pk]))
        self.assertTemplateUsed(response, 'revisions/revision_details.html')

    def test_cashier_cannot_view_revisions(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        self.client.force_login(self.cashier)
        response = self.client.get(reverse('revision_view', args=[revision.pk]))
        self.assertEqual(response.status_code, 403)
