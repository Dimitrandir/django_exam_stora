from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Enables Postgres' pg_trgm extension -- powers fast, typo-tolerant
    substring search (used by the global search bar) via GIN trigram
    indexes on the searchable model fields. Requires a DB role with
    CREATE privileges on extensions (the app's normal DB user usually
    isn't enough on managed Postgres; may need a superuser to run this
    once)."""

    initial = True

    dependencies = []

    operations = [
        TrigramExtension(),
    ]
