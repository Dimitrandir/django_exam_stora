import socket
from unittest.mock import MagicMock

from django.test import TestCase, override_settings
from django.urls import reverse

from STORA.accounts.models import Employee
from STORA.core.utils import dispatch_task
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

    def test_product_label_includes_stock_and_prices_with_a_plain_dash(self):
        # Code - Name - Stock - Delivery price - Sell price, "-" throughout
        # (used to be an em dash between just code and name) -- delivery
        # price is None here (never delivered), shown as "-" too, not "0.00".
        self.client.force_login(self.user)
        response = self._search('sparkl')
        labels = [item['label'] for item in response.json()['products']]
        self.assertIn('P9000001 - Sparkling Water - 0 pcs - - - 1.50', labels)
        self.assertNotIn('—', labels[0])

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
        # Employees are Manager-only in search (same gate as EmployeeListView).
        self.user.role = Employee.MANAGER
        self.user.save()
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


class DispatchTaskTests(TestCase):
    """dispatch_task() must never let an unreachable Celery broker stall the
    request -- see core/utils.py for why a raw TCP probe is used instead of
    trusting Celery/Kombu's own connection-timeout settings (they turned out
    not to actually bound the delay; kombu's Connection.connect() has its
    own hardcoded ~2s retry sleep that no documented setting overrides)."""

    def test_skips_delay_when_broker_port_is_closed(self):
        # Port 1 is a privileged port basically guaranteed to have nothing
        # listening on it locally -- connection is refused near-instantly.
        with override_settings(CELERY_BROKER_URL='redis://127.0.0.1:1/0'):
            task = MagicMock()
            dispatch_task(task, 'arg')
            task.delay.assert_not_called()

    def test_calls_delay_when_broker_port_is_open(self):
        # A real listening socket (doesn't need to speak Redis -- the probe
        # only checks that *something* accepts the TCP connection) proves
        # the reachable path still hands off to Celery normally.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with override_settings(CELERY_BROKER_URL=f'redis://127.0.0.1:{port}/0'):
                task = MagicMock()
                dispatch_task(task, 'arg')
                task.delay.assert_called_once_with('arg')
        finally:
            listener.close()

class BackupDbCommandTests(TestCase):
    """manage.py backup_db -- pg_dump itself is mocked (it writes a fake file),
    everything around it (when to back up, rotation, the log) is real."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.settings_override = override_settings(
            BACKUP_DIR=str(self.dir), BACKUP_INTERVAL_MINUTES=60, BACKUP_FORCE_AFTER_HOURS=24,
            BACKUP_MAX_SIZE_GB=3, BACKUP_KEEP_MIN=7, BACKUP_PG_DUMP='/fake/pg_dump', BACKUP_NSSM='',
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def _run(self, *args, counter=100, returncode=0):
        from unittest.mock import patch
        from django.core.management import call_command

        def fake_run(command, **kwargs):
            if returncode == 0:
                out = command[command.index('-f') + 1]
                with open(out, 'wb') as f:
                    f.write(b'x' * 1000)
            return MagicMock(returncode=returncode, stderr='connection refused')

        with patch('STORA.core.management.commands.backup_db.database_change_counter', return_value=counter), \
                patch('STORA.core.management.commands.backup_db.subprocess.run', side_effect=fake_run), \
                patch('STORA.core.management.commands.backup_db.shutil.which', return_value=None):
            call_command('backup_db', *args, stdout=MagicMock())

    def _dumps(self):
        return sorted(p.name for p in self.dir.glob('stora_*.dump'))

    def _set_last_backup(self, minutes_ago, counter=100):
        import json
        from datetime import timedelta
        from django.utils import timezone
        (self.dir / '.backup_state.json').write_text(json.dumps({
            'last_backup_at': (timezone.now() - timedelta(minutes=minutes_ago)).isoformat(), 'counter': counter,
        }))

    def test_first_run_makes_a_backup_and_logs_it(self):
        self._run()
        self.assertEqual(len(self._dumps()), 1)
        self.assertIn('OK', (self.dir / 'backup.log').read_text())
        self.assertEqual(list(self.dir.glob('*.part')), [])  # renamed when complete

    def test_skips_when_too_soon(self):
        self._set_last_backup(minutes_ago=10, counter=1)
        self._run(counter=999)  # changed, but the interval hasn't passed
        self.assertEqual(self._dumps(), [])

    def test_skips_when_nothing_changed(self):
        self._set_last_backup(minutes_ago=90, counter=100)
        self._run(counter=100)
        self.assertEqual(self._dumps(), [])

    def test_backs_up_when_changed_and_interval_passed(self):
        self._set_last_backup(minutes_ago=90, counter=100)
        self._run(counter=101)
        self.assertEqual(len(self._dumps()), 1)

    def test_backs_up_without_changes_after_force_after_hours(self):
        self._set_last_backup(minutes_ago=25 * 60, counter=100)
        self._run(counter=100)
        self.assertEqual(len(self._dumps()), 1)

    def test_force_ignores_interval_and_changes(self):
        self._set_last_backup(minutes_ago=1, counter=100)
        self._run('--force', counter=100)
        self.assertEqual(len(self._dumps()), 1)

    def test_pg_dump_failure_is_logged_and_leaves_no_file(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._run(returncode=1)
        self.assertEqual(self._dumps(), [])
        self.assertEqual(list(self.dir.glob('*.part')), [])
        self.assertIn('FAILED pg_dump failed: connection refused', (self.dir / 'backup.log').read_text())

    @override_settings(BACKUP_MAX_SIZE_GB=3500 / 1024 ** 3, BACKUP_KEEP_MIN=2)
    def test_rotation_deletes_oldest_over_the_size_cap_but_keeps_the_minimum(self):
        for day in range(1, 6):  # 5 old dumps of 1000 bytes each
            (self.dir / f'stora_2026-01-0{day}_10-00.dump').write_bytes(b'x' * 1000)
        self._run('--force')  # + 1 new = 6000 bytes, cap 3500 -> keep the newest 3
        dumps = self._dumps()
        self.assertEqual(len(dumps), 3)
        self.assertNotIn('stora_2026-01-01_10-00.dump', dumps)
        self.assertIn('stora_2026-01-05_10-00.dump', dumps)

    @override_settings(BACKUP_MAX_SIZE_GB=0, BACKUP_KEEP_MIN=2)
    def test_rotation_never_goes_below_keep_min(self):
        for day in range(1, 4):
            (self.dir / f'stora_2026-01-0{day}_10-00.dump').write_bytes(b'x' * 1000)
        self._run('--force')
        self.assertEqual(len(self._dumps()), 2)

    @override_settings(BACKUP_DIR='')
    def test_missing_backup_dir_is_a_clear_error(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaisesMessage(CommandError, 'BACKUP_DIR'):
            call_command('backup_db', stdout=MagicMock())

