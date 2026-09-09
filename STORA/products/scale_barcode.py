"""Decodes EAN-13 "scale barcodes" printed by shop weighing scales: a fixed
7 or 8 digit product-code prefix, then a 5 or 4 digit quantity (grams for a
weight product, piece count for a piece product), then a standard EAN-13
check digit -- 13 digits total either way.

Which product a scanned code belongs to isn't derivable from the digits
alone (a 13-digit run can't tell you where the prefix ends), so a product's
scale-code prefix has to be registered up front as a `Barcode` row with
`is_scale_code=True` (see products/forms.py, _barcode_formset.html). This
module tries both possible prefix lengths against those registered rows and
uses whichever one matches -- see CLAUDE.md for the format writeup.
"""

from decimal import Decimal

from STORA.products.models import Barcode, Product

PREFIX_LENGTHS = (7, 8)


def ean13_check_digit(digits12: str) -> int:
    """Standard EAN-13 check digit: 1x weight on odd positions (1st, 3rd,
    ... from the left), 3x weight on even positions, summed mod 10."""
    total = 0
    for index, char in enumerate(digits12):
        digit = int(char)
        total += digit if index % 2 == 0 else digit * 3
    return (10 - (total % 10)) % 10


def is_valid_ean13(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    return ean13_check_digit(code[:12]) == int(code[12])


def decode_scale_barcode(code: str):
    """Returns (product, quantity: Decimal) if `code` is a valid,
    registered scale barcode, otherwise None. `quantity` is already in the
    unit Product.quantity/SaleItems.sale_quantity expect: kg for a weight
    product (grams / 1000), a plain count for a piece product.
    """
    code = code.strip()
    if not is_valid_ean13(code):
        return None

    for prefix_length in PREFIX_LENGTHS:
        prefix = code[:prefix_length]
        quantity_digits = code[prefix_length:12]
        barcode = Barcode.objects.filter(code=prefix, is_scale_code=True).select_related('product').first()
        if not barcode:
            continue

        product = barcode.product
        raw_quantity = int(quantity_digits)
        if product.unit_type == Product.WEIGHT:
            quantity = Decimal(raw_quantity) / Decimal(1000)
        else:
            quantity = Decimal(raw_quantity)
        return product, quantity

    return None
