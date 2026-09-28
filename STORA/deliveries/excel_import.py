"""Excel import for Deliveries -- lets a warehouse clerk load a whole batch
of goods from a spreadsheet instead of adding products to a delivery one by
one. Parsing/matching only lives here; nothing in this module touches the
database -- it just builds what the delivery grid should show. Actual
product creation happens at Save time (see deliveries.views.deliveries_add),
so reviewing/editing/cancelling the import never leaves anything behind.

Column layout (see build_template_workbook) -- only Name is required, every
other column may be blank:
  Code | Name | Unit Type | Quantity | Category | Subcategory |
  Delivery Price | Sell Price | Main Supplier | Barcode 1 | Barcode 2 |
  Barcode 3

Unit Type ("pcs"/"kg", Bulgarian "бр"/"кг" also accepted, case-insensitive)
only matters for a genuinely NEW product -- it's ignored for a code/barcode
match, since an existing product's unit_type is already fixed and this file
is never allowed to edit the catalog (see matching rules below). Left blank
or unrecognised, a new product defaults to "pcs" -- this used to be silently
hardcoded to "pcs" for every imported product regardless of what it
actually was, which is wrong for anything sold by weight (produce, deli):
Product.quantity's step (whole units vs fractional kg) and scale-barcode
decoding (products/scale_barcode.py) both depend on unit_type being right
from the moment the product is created, not fixed up later by hand.

Matching rules (worked out with the shop owner before building this):
- Code or any of the 3 barcodes matching an existing product means this row
  IS that product -- a positive quantity delivers more of it, a zero
  quantity is a no-op (the product already exists, nothing to do), never a
  price/category/etc. edit of the existing product (the file is for
  receiving goods, not bulk-editing the catalog).
- Category/Subcategory/Main Supplier are matched by name via Postgres
  trigram similarity (same tech as every other fuzzy picker in this app) --
  never auto-created when nothing matches confidently enough, just left
  blank. Getting a category wrong by guessing would make a worse mess than
  leaving it for a human to fill in.
- A row with no code/barcode match is a candidate NEW product -- also gets
  a fuzzy name check against the whole catalog (including archived
  products, since "this barcode changed but it's the same real item,
  possibly discontinued/renamed" is exactly the case worth catching) to
  flag a likely duplicate. Never auto-links either -- only flags it for the
  person reviewing the delivery grid to confirm or dismiss.
"""

from decimal import Decimal, InvalidOperation

import openpyxl
from django.contrib.postgres.search import TrigramSimilarity

from STORA.products.models import Barcode, Category, Product, Suppliers

COLUMN_HEADERS = [
    'Code', 'Name', 'Unit Type', 'Quantity', 'Category', 'Subcategory',
    'Delivery Price', 'Sell Price', 'Main Supplier',
    'Barcode 1', 'Barcode 2', 'Barcode 3',
]

# Accepted spellings for the Unit Type column, both languages, lower-cased.
_WEIGHT_SPELLINGS = {'kg', 'weight', 'кг', 'тегло', 'теглов', 'теглови'}
_PIECE_SPELLINGS = {'pcs', 'pc', 'piece', 'бр', 'бройка', 'бройки', 'бройков', 'бройкови'}


def _clean_unit_type(value):
    """Excel's Unit Type cell -> Product.WEIGHT/Product.PIECE. Blank or
    unrecognised falls back to Product.PIECE (the same default Product
    itself uses), rather than erroring out -- most goods really are sold
    by the piece, and this column is optional like every other one."""
    text = _clean_str(value).lower()
    if text in _WEIGHT_SPELLINGS:
        return Product.WEIGHT
    return Product.PIECE

# How confident a trigram match needs to be before it's used at all (to
# auto-fill category/supplier) or shown as a possible-duplicate flag.
# Empirical, not derived from anything -- high enough that unrelated names
# don't match by coincidence, low enough to still catch a reordered/
# reworded name ("Капсули Ариел" vs "Ариел Капсули").
SIMILARITY_THRESHOLD = 0.35


def build_template_workbook():
    """Blank .xlsx with just the header row -- served by
    delivery_import_template for "Download template"."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Import'
    ws.append(COLUMN_HEADERS)
    for col_index in range(1, len(COLUMN_HEADERS) + 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_index)].width = 16
    return wb


def _clean_str(value):
    if value is None:
        return ''
    return str(value).strip()


def _clean_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value).strip().replace(',', '.'))
    except InvalidOperation:
        return None


def _best_name_match(name, queryset):
    """Best row in `queryset` whose `name` is trigram-similar enough to
    `name`, or None. `queryset` must be a model with a `name` field."""
    if not name:
        return None
    return (
        queryset.annotate(similarity=TrigramSimilarity('name', name))
        .filter(similarity__gte=SIMILARITY_THRESHOLD)
        .order_by('-similarity')
        .first()
    )


def parse_delivery_import(file_obj):
    """Reads an uploaded .xlsx and resolves every row against the current
    catalog. Returns {'rows': [...], 'skipped': [...], 'errors': [...]}.

    Each entry in `rows` is either:
      {'row_number', 'is_new': False, 'product_id', 'quantity'}
    for a code/barcode match, or:
      {'row_number', 'is_new': True, 'internal_code', 'name', 'unit_type',
       'quantity', 'delivery_price', 'sell_price', 'category_id',
       'category_name', 'supplier_id', 'supplier_name', 'barcodes': [...],
       'duplicate_candidate': {'id', 'name'} | None}
    for a genuinely new product candidate."""
    try:
        wb = openpyxl.load_workbook(file_obj, data_only=True, read_only=True)
    except Exception:
        return {'rows': [], 'skipped': [], 'errors': ['Could not read this file -- is it a real .xlsx?']}
    ws = wb.active

    rows_out = []
    skipped = []
    errors = []
    next_code_counter = None

    all_rows = list(ws.iter_rows(values_only=True))
    if len(all_rows) <= 1:
        return {'rows': [], 'skipped': [], 'errors': ['The file has no data rows.']}

    for row_number, raw_row in enumerate(all_rows[1:], start=2):
        if raw_row is None or all(cell in (None, '') for cell in raw_row):
            continue

        cells = list(raw_row) + [None] * (len(COLUMN_HEADERS) - len(raw_row))
        (code, name, unit_type, quantity, category_name, subcategory_name,
         delivery_price, sell_price, supplier_name, bc1, bc2, bc3) = cells[:12]

        code = _clean_str(code)
        name = _clean_str(name)
        if not name:
            errors.append(f'Row {row_number}: name is required, row skipped.')
            continue

        unit_type = _clean_unit_type(unit_type)
        quantity = _clean_decimal(quantity) or Decimal('0')
        delivery_price = _clean_decimal(delivery_price)
        sell_price = _clean_decimal(sell_price)
        barcodes = [b for b in (_clean_str(bc1), _clean_str(bc2), _clean_str(bc3)) if b]

        matched_product = None
        if code:
            matched_product = Product.objects.filter(internal_code=code).first()
        if not matched_product and barcodes:
            barcode_match = Barcode.objects.filter(code__in=barcodes).select_related('product').first()
            if barcode_match:
                matched_product = barcode_match.product

        if matched_product:
            if quantity <= 0:
                skipped.append(f'Row {row_number}: "{matched_product.name}" already exists, skipped (quantity was 0).')
                continue
            rows_out.append({
                'row_number': row_number,
                'is_new': False,
                'product_id': matched_product.pk,
                'quantity': float(quantity),
            })
            continue

        duplicate = _best_name_match(name, Product.objects.all())

        category = _best_name_match(category_name, Category.objects.filter(parent__isnull=True)) if category_name else None
        subcategory = None
        if subcategory_name:
            sub_qs = Category.objects.filter(parent_id=category.pk) if category else Category.objects.exclude(parent__isnull=True)
            subcategory = _best_name_match(subcategory_name, sub_qs)
        resolved_category = subcategory or category

        supplier = _best_name_match(supplier_name, Suppliers.objects.all()) if supplier_name else None

        if not code:
            if next_code_counter is None:
                from STORA.products.views import get_free_internal_codes
                next_code_counter = get_free_internal_codes()[1]
            code = str(next_code_counter)
            next_code_counter += 1

        rows_out.append({
            'row_number': row_number,
            'is_new': True,
            'internal_code': code,
            'name': name,
            'unit_type': unit_type,
            'quantity': float(quantity),
            'delivery_price': float(delivery_price) if delivery_price is not None else None,
            'sell_price': float(sell_price) if sell_price is not None else None,
            'category_id': resolved_category.pk if resolved_category else None,
            'category_name': resolved_category.name if resolved_category else '',
            'supplier_id': supplier.pk if supplier else None,
            'supplier_name': supplier.name if supplier else '',
            'barcodes': barcodes,
            'duplicate_candidate': {'id': duplicate.pk, 'name': duplicate.name} if duplicate else None,
        })

    return {'rows': rows_out, 'skipped': skipped, 'errors': errors}
