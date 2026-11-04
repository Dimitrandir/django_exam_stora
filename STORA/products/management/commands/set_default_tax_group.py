"""One-off cleanup: gives the shop's default VAT group (TaxGroup.default(),
"Б") to every product that currently has NO tax group -- e.g. products
imported from Excel before the import learned to set one. Products that
already have any group are never touched.

    python manage.py set_default_tax_group            # shows the count, asks to confirm
    python manage.py set_default_tax_group --dry-run  # only shows what it WOULD do
    python manage.py set_default_tax_group --yes      # no question (for scripts)

One UPDATE query, so like the other bulk changes it bypasses model signals
(no ProductChangeLog rows are written for it).
"""
from django.core.management.base import BaseCommand, CommandError

from STORA.products.models import Product, TaxGroup


class Command(BaseCommand):
    help = 'Set the default VAT group on every product that has none.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only show how many products would change.')
        parser.add_argument('--yes', action='store_true', help='Do not ask for confirmation.')

    def handle(self, *args, **options):
        group = TaxGroup.default()
        if group is None:
            raise CommandError(f'No tax group named "{TaxGroup.DEFAULT_NAME}" exists -- nothing to apply.')

        products = Product.objects.filter(tax_group__isnull=True)
        count = products.count()
        self.stdout.write(f'{count} product(s) have no VAT group. Default group: {group}.')
        if count == 0:
            return
        if options['dry_run']:
            self.stdout.write('Dry run -- nothing changed.')
            return
        if not options['yes'] and input(f'Set "{group.name}" on all {count}? [y/N] ').strip().lower() != 'y':
            self.stdout.write('Cancelled -- nothing changed.')
            return

        updated = products.update(tax_group=group)
        self.stdout.write(self.style.SUCCESS(f'Done: {updated} product(s) now have "{group.name}".'))
