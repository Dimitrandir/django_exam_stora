"""PostgreSQL backend that skips Django's minimum-server-version check.

Django 6 refuses to connect to anything older than PostgreSQL 14, but the
SuperHosting.bg shared plan only offers PostgreSQL 10.23. Run against a real
10.23 server, every migration applies and the full test suite passes, so the
only thing in the way is the version check itself. This backend turns off
just that check; everything else is the stock postgresql backend.

Opt-in only, via DB_ENGINE=STORA.db_backends.postgresql_legacy in .env (see
DEPLOYMENT_SUPERHOSTING.md). Officially unsupported by Django: re-run the
tests against PostgreSQL 10 before every Django upgrade on that host.
"""

from django.db.backends.postgresql.base import DatabaseWrapper as PostgresDatabaseWrapper


class DatabaseWrapper(PostgresDatabaseWrapper):
    def check_database_version_supported(self):
        pass
