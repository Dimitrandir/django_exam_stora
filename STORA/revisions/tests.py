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


class RevisionSetItemTests(TestCase):
    """revision_set_item / RevisionItemsManager.set_count -- backs the
    directly-editable Found Qty cell (see revision_detail.html), a
    deliberate "this is the exact count" entry, unlike add_count's
    +N-per-scan semantics. Fixes the reported bug: a search-added product
    used to land at a fixed found_quantity=1 with no way to correct it."""

    def setUp(self):
        self.warehouse = User.objects.create_user(username='rev_wh3', password='pass12345', role=User.WAREHOUSE)
        self.product = Product.objects.create(
            internal_code='REV0003', name='Flour 1kg', sell_price=Decimal('3.00'),
            delivery_price=Decimal('1.80'), quantity=Decimal('5.000'),
        )
        self.client.force_login(self.warehouse)

    def test_set_count_creates_row_at_exact_value(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        response = self.client.post(reverse('revision_set_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['found_quantity'], 0)
        self.assertEqual(response.json()['unit_type'], 'piece')

    def test_set_count_overwrites_rather_than_adds(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('1'))
        response = self.client.post(reverse('revision_set_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '12'})
        self.assertEqual(response.json()['found_quantity'], 12)
        # Confirms it's a SET, not an add on top of the earlier 1 (would be 13).
        item = RevisionItems.objects.get(revision=revision, product=self.product)
        self.assertEqual(item.found_quantity, Decimal('12'))

    def test_set_count_allows_correcting_down_to_a_smaller_number(self):
        # add_count would reject a negative delta outright -- set_count is
        # exactly for this: typing a smaller, corrected number directly.
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        RevisionItems.objects.add_count(revision, self.product, Decimal('10'))
        response = self.client.post(reverse('revision_set_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '3'})
        self.assertEqual(response.json()['found_quantity'], 3)

    def test_set_count_rejects_negative(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        response = self.client.post(reverse('revision_set_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '-1'})
        self.assertEqual(response.status_code, 400)

    def test_set_count_rejected_once_revision_is_completed(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.save(update_fields=['status'])
        response = self.client.post(reverse('revision_set_item', args=[revision.pk]),
                                     {'product_id': self.product.pk, 'quantity': '5'})
        self.assertEqual(response.status_code, 409)


class RevisionNamingTests(TestCase):
    def setUp(self):
        self.warehouse = User.objects.create_user(username='rev_wh4', password='pass12345', role=User.WAREHOUSE)
        self.client.force_login(self.warehouse)

    def test_start_with_a_name_saves_it(self):
        response = self.client.post(reverse('revision_start'), {'name': 'Fridge only'})
        revision = RevisionAttributes.objects.get()
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        self.assertEqual(revision.name, 'Fridge only')

    def test_start_without_a_name_leaves_it_blank(self):
        self.client.post(reverse('revision_start'))
        revision = RevisionAttributes.objects.get()
        self.assertEqual(revision.name, '')

    def test_rename_updates_an_open_revision(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        response = self.client.post(reverse('revision_rename', args=[revision.pk]), {'name': 'Weekly full count'})
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        revision.refresh_from_db()
        self.assertEqual(revision.name, 'Weekly full count')

    def test_rename_also_works_on_a_completed_revision(self):
        # Naming is a bookkeeping tweak, not a stock-affecting action -- it
        # shouldn't require the revision to still be open, unlike every
        # other action in this app.
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.save(update_fields=['status'])
        response = self.client.post(reverse('revision_rename', args=[revision.pk]), {'name': 'Q3 count'})
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        revision.refresh_from_db()
        self.assertEqual(revision.name, 'Q3 count')


class RevisionPreloadTests(TestCase):
    """?preload=<id>,<id>,... on revision_home -- the "Load into Revision"
    bulk action from the Products grid. Joins the open revision if there is
    one, otherwise starts one on the spot; each product lands at
    found_quantity=0 (editable immediately), never resetting a product
    that's already being counted."""

    def setUp(self):
        self.warehouse = User.objects.create_user(username='rev_wh5', password='pass12345', role=User.WAREHOUSE)
        self.client.force_login(self.warehouse)

    def test_preload_with_no_open_revision_starts_one_and_adds_products(self):
        product = Product.objects.create(
            internal_code='REV0004', name='Preload Product', sell_price=Decimal('4.00'),
            delivery_price=Decimal('2.00'), quantity=Decimal('3.000'),
        )
        response = self.client.get(reverse('revision_home'), {'preload': str(product.pk)})
        revision = RevisionAttributes.objects.get()
        self.assertRedirects(response, reverse('revision_view', args=[revision.pk]))
        item = RevisionItems.objects.get(revision=revision, product=product)
        self.assertEqual(item.found_quantity, Decimal('0'))
        self.assertEqual(item.system_quantity_at_start, Decimal('3.000'))

    def test_preload_with_open_revision_joins_it(self):
        existing = RevisionAttributes.objects.create(started_by=self.warehouse)
        product = Product.objects.create(
            internal_code='REV0005', name='Preload Product 2', sell_price=Decimal('4.00'), quantity=Decimal('1.000'),
        )
        response = self.client.get(reverse('revision_home'), {'preload': str(product.pk)})
        self.assertRedirects(response, reverse('revision_view', args=[existing.pk]))
        self.assertEqual(RevisionAttributes.objects.count(), 1)
        self.assertTrue(RevisionItems.objects.filter(revision=existing, product=product).exists())

    def test_preload_does_not_reset_an_already_counted_product(self):
        revision = RevisionAttributes.objects.create(started_by=self.warehouse)
        product = Product.objects.create(
            internal_code='REV0006', name='Preload Product 3', sell_price=Decimal('4.00'), quantity=Decimal('1.000'),
        )
        RevisionItems.objects.add_count(revision, product, Decimal('9'))
        self.client.get(reverse('revision_home'), {'preload': str(product.pk)})
        item = RevisionItems.objects.get(revision=revision, product=product)
        self.assertEqual(item.found_quantity, Decimal('9'))

    def test_preload_without_the_param_renders_the_normal_home_page(self):
        response = self.client.get(reverse('revision_home'))
        self.assertTemplateUsed(response, 'revisions/revision_home.html')
