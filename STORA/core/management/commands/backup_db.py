"""manage.py backup_db -- the database (and config) backup.

Meant to be run OFTEN by Windows Task Scheduler (every 15 minutes, see
DEPLOYMENT.md section 8); the command itself decides whether a backup is due,
from the BACKUP_* settings in .env:

  * not more often than BACKUP_INTERVAL_MINUTES,
  * and only if something in the database changed since the last backup,
  * except that after BACKUP_FORCE_AFTER_HOURS it backs up anyway.

"Did anything change" comes from PostgreSQL's own per-table counters of
inserted/updated/deleted rows (pg_stat_user_tables), compared with the value
saved at the last backup. django_session is left out of that count -- it is
written on almost every page view and draft autosave, so with it the
database would never look unchanged. The counters restart from zero when
PostgreSQL restarts; that only causes one extra backup (the safe direction).

pg_dump takes a consistent snapshot of the database without blocking sales
or anything else that writes while it runs (it only makes a migration wait).

Each backup is a compressed pg_dump file (restore with pg_restore) in
BACKUP_DIR, which is meant to be a folder inside OneDrive so it gets
uploaded on its own. Next to it, in config/, the latest copy of .env and the
Windows service settings (nssm dump), so a new machine can be set up again
from the backup folder alone. Old dumps are deleted, oldest first, once all
of them together go over BACKUP_MAX_SIZE_GB -- never the newest
BACKUP_KEEP_MIN. Every backup, failure and deletion goes to backup.log in
the same folder.
"""
import glob
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

STATE_FILE = '.backup_state.json'
LOG_FILE = 'backup.log'
CONFIG_DIR = 'config'
DUMP_PREFIX = 'stora_'
DUMP_SUFFIX = '.dump'
SERVICE_NAME = 'STORA'


def shop_now():
    """Current time in the shop's own time zone -- TIME_ZONE is UTC, which
    would make file names and log lines 2-3 hours off from the wall clock."""
    return timezone.now().astimezone(ZoneInfo(settings.FISCAL_DEVICE_TIME_ZONE))


def database_change_counter():
    """Total rows ever inserted/updated/deleted in our tables (since the
    PostgreSQL statistics were last reset), django_session excluded."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(SUM(n_tup_ins + n_tup_upd + n_tup_del), 0) "
            "FROM pg_stat_user_tables WHERE relname <> 'django_session'"
        )
        return int(cursor.fetchone()[0])


def find_pg_dump():
    if settings.BACKUP_PG_DUMP:
        return settings.BACKUP_PG_DUMP
    on_path = shutil.which('pg_dump')
    if on_path:
        return on_path
    # Windows installs aren't on PATH by default -- take the newest version.
    candidates = glob.glob(r'C:\Program Files\PostgreSQL\*\bin\pg_dump.exe')

    def version(path):
        match = re.search(r'PostgreSQL\\(\d+)', path)
        return int(match.group(1)) if match else 0

    return max(candidates, key=version) if candidates else None


class Command(BaseCommand):
    help = 'Backs up the database (and .env) when one is due -- see the module docstring.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='Back up now, ignoring the interval and the "no changes" check.')

    def handle(self, *args, force=False, **options):
        if not settings.BACKUP_DIR:
            raise CommandError('BACKUP_DIR is not set in .env -- nowhere to put the backup.')
        self.backup_dir = Path(settings.BACKUP_DIR)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        state = self._load_state()
        counter = database_change_counter()
        reason = self._reason_to_back_up(state, counter, force)
        if reason is None:
            # Not logged to backup.log -- with a run every 15 minutes it
            # would drown out the lines that matter.
            self.stdout.write('No backup needed right now.')
            return

        # The counter is read BEFORE the dump on purpose: a sale that lands
        # while pg_dump runs then still counts as "changed" next time.
        target = self._dump()
        size_mb = target.stat().st_size / 1024 / 1024
        self._save_state({'last_backup_at': timezone.now().isoformat(), 'counter': counter,
                          'last_file': target.name})
        self._backup_config()
        deleted = self._rotate()

        message = f'OK {target.name} ({size_mb:.1f} MB, {reason})'
        if deleted:
            message += f'; deleted old: {", ".join(deleted)}'
        self._log(message)
        self.stdout.write(self.style.SUCCESS(message))

    # -- deciding ---------------------------------------------------------

    def _reason_to_back_up(self, state, counter, force):
        if force:
            return 'forced'
        last_at = state.get('last_backup_at')
        if not last_at:
            return 'first backup'
        elapsed = timezone.now() - datetime.fromisoformat(last_at)
        if elapsed < timedelta(minutes=settings.BACKUP_INTERVAL_MINUTES):
            return None
        if elapsed >= timedelta(hours=settings.BACKUP_FORCE_AFTER_HOURS):
            return f'no backup for {settings.BACKUP_FORCE_AFTER_HOURS}h'
        if counter != state.get('counter'):
            return 'database changed'
        return None

    # -- the dump ---------------------------------------------------------

    def _dump(self):
        pg_dump = find_pg_dump()
        if not pg_dump:
            self._fail('pg_dump not found -- set BACKUP_PG_DUMP in .env to its full path.')

        db = settings.DATABASES['default']
        stamp = shop_now().strftime('%Y-%m-%d_%H-%M')
        target = self.backup_dir / f'{DUMP_PREFIX}{stamp}{DUMP_SUFFIX}'
        # Written under another name first and renamed only when complete --
        # OneDrive never uploads, and the rotation never counts, a half file.
        partial = target.with_name(target.name + '.part')
        env = dict(os.environ, PGPASSWORD=db['PASSWORD'] or '')
        command = [
            pg_dump, '-h', db['HOST'] or '127.0.0.1', '-p', str(db['PORT'] or 5432), '-U', db['USER'],
            '-w',          # never ask for a password -- an unattended run must fail, not hang
            '-F', 'c',     # compressed custom format, restored with pg_restore
            '-f', str(partial), db['NAME'],
        ]
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            partial.unlink(missing_ok=True)
            self._fail(f'pg_dump could not run: {exc}')
        if result.returncode != 0:
            partial.unlink(missing_ok=True)
            self._fail(f'pg_dump failed: {result.stderr.strip() or result.returncode}')
        os.replace(partial, target)
        return target

    # -- config -----------------------------------------------------------

    def _backup_config(self):
        """Latest .env + Windows service settings, overwritten each time (a
        config history isn't needed, just "what it is now"). A failure here
        is logged but never fails the database backup itself."""
        config_dir = self.backup_dir / CONFIG_DIR
        config_dir.mkdir(exist_ok=True)
        env_file = Path(settings.BASE_DIR) / '.env'
        try:
            if env_file.exists():
                shutil.copy2(env_file, config_dir / '.env')
        except OSError as exc:
            self._log(f'WARNING could not copy .env: {exc}')

        nssm = settings.BACKUP_NSSM or shutil.which('nssm')
        if not nssm:
            return
        try:
            result = subprocess.run([nssm, 'dump', SERVICE_NAME], capture_output=True, timeout=30)
            raw = result.stdout
            # nssm may print UTF-16 on Windows -- recognisable by NUL bytes.
            text = raw.decode('utf-16', errors='replace') if b'\x00' in raw else raw.decode('utf-8', errors='replace')
            if result.returncode == 0 and text.strip():
                (config_dir / f'nssm_{SERVICE_NAME}.txt').write_text(text, encoding='utf-8')
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log(f'WARNING could not save the service settings (nssm): {exc}')

    # -- rotation ---------------------------------------------------------

    def _rotate(self):
        """Oldest first, until the dumps fit in BACKUP_MAX_SIZE_GB -- but the
        newest BACKUP_KEEP_MIN always stay. File names sort by date."""
        dumps = sorted(self.backup_dir.glob(f'{DUMP_PREFIX}*{DUMP_SUFFIX}'))
        limit = settings.BACKUP_MAX_SIZE_GB * 1024 ** 3
        total = sum(path.stat().st_size for path in dumps)
        deleted = []
        while total > limit and len(dumps) > settings.BACKUP_KEEP_MIN:
            oldest = dumps.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink()
            deleted.append(oldest.name)
        return deleted

    # -- helpers ----------------------------------------------------------

    def _load_state(self):
        try:
            return json.loads((self.backup_dir / STATE_FILE).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        (self.backup_dir / STATE_FILE).write_text(json.dumps(state), encoding='utf-8')

    def _log(self, message):
        stamp = shop_now().strftime('%Y-%m-%d %H:%M')
        with open(self.backup_dir / LOG_FILE, 'a', encoding='utf-8') as log:
            log.write(f'{stamp} {message}\n')

    def _fail(self, message):
        self._log(f'FAILED {message}')
        raise CommandError(message)
