"""The "What's new" list on the home screen (templates/index.html).

Newest first. Each entry is a date and a few short points written for the
people using the shop screens -- what they will notice, not how it was
built. Source text in English inside gettext_lazy (`_`), the Bulgarian is in
locale/bg/LC_MESSAGES/django.po like every other string (makemessages picks
these up). Adding an entry: put a new dict at the TOP of the list, then run
makemessages/compilemessages (see CLAUDE.md, "Превод на интерфейса"). It
reaches the shops with the normal `git pull` -- no migration, nothing in the
database.
"""
from datetime import date

from django.utils.translation import gettext_lazy as _

# Entries this many days old (or newer) get the "New" mark on the home screen.
RECENT_DAYS = 7

WHATS_NEW = [
    {
        'date': date(2026, 11, 4),
        'points': [
            _('Deliveries, write-offs and scrap: scanning a barcode while a quantity box is open now adds 1 to that '
              'product, instead of typing the barcode in as the quantity.'),
            _('After you scan a new product, the cursor waits in its quantity box, so you can type the count '
              'straight away.'),
            _('Enter on the last box of a row takes you back to the search box for the next product.'),
            _('The hidden character some barcode scanners send before every code no longer breaks the scan.'),
        ],
    },
    {
        'date': date(2026, 11, 1),
        'points': [
            _('The database is backed up to OneDrive automatically every hour, whenever something has changed.'),
        ],
    },
    {
        'date': date(2026, 10, 31),
        'points': [
            _('New "All refunds" list, showing whether the fiscal receipt of each refund was printed.'),
            _('A refund whose fiscal receipt did not print now says why, and has a button to print it.'),
            _('X report now sends the fiscal device its own "report without reset" command.'),
        ],
    },
    {
        'date': date(2026, 10, 28),
        'points': [
            _('Deliveries, write-offs and scrap: up to 3 open at the same time. Switch between them with the 1/2/3 '
              'buttons next to the title.'),
            _('Closing a delivery or a sale that still has products in it now asks first.'),
            _('Products added from an Excel import stay in the delivery after a failed save or a page reload.'),
        ],
    },
    {
        'date': date(2026, 10, 24),
        'points': [
            _('Products: a "Clear filters" button, so a filter on a hidden column cannot hide products unnoticed.'),
            _('Excel import: new VAT group column. Left empty, the product gets group Б.'),
        ],
    },
    {
        'date': date(2026, 10, 23),
        'points': [
            _('Products: set an attribute (color, size...) for many selected products at once.'),
            _('Products: the actions for selected products fit on one row, and "Load into" is a single menu.'),
            _('Excel import: two new products with the same name or barcode in one file no longer stop the whole '
              'delivery.'),
        ],
    },
    {
        'date': date(2026, 10, 20),
        'points': [
            _('Product attributes can be filled in from the Excel import and edited directly in the Products list.'),
        ],
    },
]


def whats_new_for_display(today):
    """The list for the template, each entry marked `is_recent` when it is
    at most RECENT_DAYS old."""
    return [
        {**entry, 'is_recent': (today - entry['date']).days <= RECENT_DAYS}
        for entry in WHATS_NEW
    ]
