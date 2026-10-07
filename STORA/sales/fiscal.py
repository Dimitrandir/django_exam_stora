"""Fiscal-printer integration (Daisy Perfect S01, via its ECRCommApp JSON
API). ECRCommApp is spawned fresh around each print and killed right after
-- confirmed live (kasov_aparat.md) that holding the COM port open
continuously would starve RACS (the store's separate access-control system),
which also needs the same port at times. Field names/formats below come
straight from kasov_aparat_ECRCommApp_Guide.pdf (repo root), verified
against the real device on 2026-09-25 -- not guessed.

Only call `attempt_print` (never `print_fiscal_receipt` directly) from a
view -- it never raises, so a fiscal failure can never block a sale that's
already been saved to the DB.
"""
import logging

from django.conf import settings
from django.utils import timezone

from fiscal_bridge import ecr_runner

logger = logging.getLogger(__name__)

# The actual talking to ECRCommApp lives in fiscal_bridge/ecr_runner.py
# (no Django there), so the cloud install's bridge on the till PC can run
# the exact same code. This module only BUILDS the command list for a
# receipt/report from STORA's own records, then runs it locally through
# the wrappers below.
FiscalPrintError = ecr_runner.FiscalPrintError


def _device_config():
    return ecr_runner.DeviceConfig(
        api_url=settings.FISCAL_API_URL,
        com_port=settings.FISCAL_COM_PORT,
        ecrcommapp_path=settings.FISCAL_ECRCOMMAPP_PATH,
        debug=settings.FISCAL_DEBUG,
    )


def _post_command(cmd, cmd_data, timeout=15):
    return ecr_runner.post_command(_device_config(), cmd, cmd_data, timeout=timeout)


def _start_ecrcommapp():
    return ecr_runner.start_ecrcommapp(_device_config())


def _stop_ecrcommapp(process):
    ecr_runner.stop_ecrcommapp(process)


def _run_commands(commands):
    """Runs `commands` on the printer attached to THIS machine. Looks the
    three wrappers up at call time, so tests can mock them out."""
    return ecr_runner.run_commands(commands, post=_post_command, start=_start_ecrcommapp, stop=_stop_ecrcommapp)


def _next_unic_sale_num():
    from .models import FiscalCounter
    seq = FiscalCounter.next()
    operator = str(settings.FISCAL_OPERATOR_NUM).zfill(2)
    return f'{settings.FISCAL_DEVICE_SERIAL}-OP{operator}-{seq:07d}'


def _tax_letter_for(product):
    tax_group = product.tax_group
    if tax_group is None or not tax_group.fiscal_letter:
        raise FiscalPrintError(f'Product "{product.name}" has no fiscal tax-group letter set (Tax Groups screen).')
    return tax_group.fiscal_letter


def _validate_items(sale):
    """Checked BEFORE the receipt is opened on the device -- a bad tax-group
    mapping should never leave a half-open receipt to clean up."""
    items = list(sale.items.select_related('sale_item__tax_group'))
    if not items:
        raise FiscalPrintError('Sale has no items to fiscalize.')
    for item in items:
        _tax_letter_for(item.sale_item)
    return items


def _receipt_commands(start_cmd_data, items, sale_type, amount_in, barcode_data=None):
    """Builds the shared open -> sell -> total -> close sequence for both a real sale
    and a storno -- only what differs between them (FDStartFiscRcp's extra
    fields, whether each line is a Sale or Refund, the paid/refunded amount)
    is passed in. `items` is a list of {'name', 'tax_letter', 'price', 'qty'}
    dicts. `barcode_data`, if given, prints a Code128 barcode on the receipt
    (FDPrintBarcode requires an open receipt, same as FDSaleItem -- can't be
    printed standalone after the fact) -- used so a printed sale receipt can
    later be scanned straight into the "Refund a Sale" search screen (its
    numeric-query handling already treats a bare number as an exact sale ID,
    see refund_find). Returns the command list -- see _run_commands; if
    any of it fails once the receipt is open, ecr_runner.run_commands
    cancels the receipt on the device."""
    commands = [{'cmd': 'FDStartFiscRcp', 'data': start_cmd_data}]

    for item in items:
        commands.append({'cmd': 'FDSaleItem', 'data': {
            'Text1': item['name'][:28],
            'Text2': '',
            'TaxGrp': item['tax_letter'],
            'Sale type': sale_type,
            'Price': str(item['price']),
            'Qty': str(item['qty']),
            'Percent': '0',
            'Netto': '0',
        }})

    commands.append({'cmd': 'FDTotalSum', 'data': {
        'Text1': '', 'Text2': '',
        'Payment type': 'В Брой',
        'AmountIn': str(amount_in),
    }})

    if barcode_data:
        # Moved here (after FDTotalSum, before FDEndFiscRcp) to match a
        # real reference receipt the user photographed from another
        # store -- on paper the barcode sits between "В брой евро" and
        # "Ресто", meaning change is printed when the receipt actually
        # closes (FDEndFiscRcp), not as part of FDTotalSum itself. Used
        # to run right after the item loop, before FDTotalSum, which
        # printed it above the total/payment lines instead. NOT YET
        # CONFIRMED LIVE on our own device (no physical printer attached
        # in this environment) -- verify the barcode actually lands
        # between those two lines next real test print, same as every
        # other field here that started as "inferred" before being
        # confirmed.
        commands.append({'cmd': 'FDPrintBarcode', 'data': {
            # Type is the device's own NUMERIC barcode-type code, not
            # the readable name -- confirmed live (2026-09-28): sending
            # the string "Code128" here made FDPrintBarcode fail
            # outright with a service-level error, on a receipt that
            # had already opened and sold an item fine. The guide's own
            # worked example uses Type "1" with 7-digit data, matching
            # EAN8's documented "7 bytes" spec exactly, so "3" for
            # Code128 here is inferred from that table's row order
            # (1=EAN8, 2=EAN13, 3=Code128, ...), not from an explicit
            # numbered table in the doc (none exists) -- confirm via
            # ECRWebApp's own FDPrintBarcode dropdown before trusting
            # it blindly, same way Refund="R" and Reason 0/1/2 were
            # confirmed rather than guessed.
            # Scale -- "width of the barcode's thinnest line, in
            # pixels; 0 uses the device's own default width" per the
            # guide's own field description (not a documented default
            # value, just "0 = default"). A small explicit value prints
            # thinner than that default, asked for live after the same
            # photo comparison. Also not yet confirmed live -- if 2px
            # turns out too thin to scan reliably on the real printer,
            # go up from there rather than back to 0.
            'Type': '3', 'Data': barcode_data, 'Pos': 'C', 'Scale': 2, 'High': 0,
            # PrnText is a JSON bool here, not the string '0'/'1' the
            # doc's own field description implies -- the worked example
            # sends `"PrnText": true` literally; also confirmed live
            # that the string form was part of what failed.
            'PrnText': True,
        }})

    commands.append({'cmd': 'FDEndFiscRcp', 'data': {}})
    return commands


def print_fiscal_receipt(sale):
    """Prints a real fiscal receipt for `sale` on the Daisy Perfect S01.
    Raises FiscalPrintError on any failure. Only meaningful for CASH sales
    right now (see sales_add) -- CARD/MIXED have no fiscal Payment type
    mapped yet. Returns {'unic_sale_num', 'receipt_number'} -- the latter is
    needed later to storno this exact receipt (see print_fiscal_refund).
    Callers that must not let a failure here block anything (i.e. every real
    caller) should use `attempt_print` instead."""
    if not settings.FISCAL_ENABLED:
        raise FiscalPrintError('Fiscal printing is disabled (FISCAL_ENABLED).')

    sale_items = _validate_items(sale)
    unic_sale_num = _next_unic_sale_num()
    items = [
        {'name': i.sale_item.name, 'tax_letter': _tax_letter_for(i.sale_item),
         'price': i.price_at_sale, 'qty': i.sale_quantity}
        for i in sale_items
    ]
    start_data = {
        'Operator': int(settings.FISCAL_OPERATOR_NUM),
        'Password': int(settings.FISCAL_OPERATOR_PASSWORD),
        'UnicSaleNum': unic_sale_num,
        'Invoice': '', 'Refund': '', 'Credit': '',
        'Reason': 0, 'DocLink': 0, 'DocLinkDT': '', 'FiskMem': '', 'InvLink': '',
    }
    commands = _receipt_commands(start_data, items, 'Sale', sale.amount_paid, barcode_data=str(sale.pk))
    end_result = _run_commands(commands)[-1]
    return {'unic_sale_num': unic_sale_num, 'receipt_number': end_result.get('FiscReceipt')}


def attempt_print(sale):
    """Never raises -- safe to call right after a sale is saved. Records the
    outcome on the sale itself (fiscal_status/fiscal_error/...) instead."""
    from .models import SaleAttributes

    if settings.FISCAL_DEBUG:
        logger.info('Fiscal: attempting print for Sale #%s on %s', sale.pk, settings.FISCAL_COM_PORT)
    try:
        result = print_fiscal_receipt(sale)
    except FiscalPrintError as exc:
        sale.fiscal_status = SaleAttributes.FISCAL_FAILED
        sale.fiscal_error = str(exc)
        sale.save(update_fields=['fiscal_status', 'fiscal_error'])
        logger.warning('Fiscal print failed for Sale #%s: %s', sale.pk, exc)
    else:
        sale.fiscal_status = SaleAttributes.FISCAL_PRINTED
        sale.fiscal_error = ''
        sale.fiscal_unic_sale_num = result['unic_sale_num']
        sale.fiscal_receipt_number = result['receipt_number']
        sale.fiscal_printed_at = timezone.now()
        sale.save(update_fields=[
            'fiscal_status', 'fiscal_error', 'fiscal_unic_sale_num', 'fiscal_receipt_number', 'fiscal_printed_at',
        ])
        if settings.FISCAL_DEBUG:
            logger.info('Fiscal: Sale #%s printed -- receipt #%s', sale.pk, result['receipt_number'])


# Storno Reason values, confirmed live against the real ECRWebApp dropdown
# (0/1/2, in this exact order) -- matches RefundAttributes.REASON_CHOICES'
# own order 1:1 by design (see that model's docstring), spelled out
# explicitly here anyway rather than relying on list position, so a future
# reordering of REASON_CHOICES can't silently send the wrong Reason to the
# device. Values are STRINGS ("0"/"1"/"2"), not ints -- confirmed live
# (2026-09-28) against the guide's own worked FDStartFiscRcp storno example,
# which sends "Reason": "0" -- an int here made the whole request fail at
# the outer service level before ever reaching the device's own error table.
def _storno_reason_map():
    from .models import RefundAttributes
    return {
        RefundAttributes.RETURN_COMPLAINT: '0',
        RefundAttributes.OPERATOR_ERROR: '1',
        RefundAttributes.TAX_BASE_REDUCTION: '2',
    }


def _validate_refund_items(refund):
    items = list(refund.items.select_related('original_item__sale_item__tax_group'))
    if not items:
        raise FiscalPrintError('Refund has no items to fiscalize.')
    for item in items:
        _tax_letter_for(item.original_item.sale_item)
    return items


def print_fiscal_refund(refund):
    """Prints a real storno fiscal receipt for `refund`, referencing the
    original sale's own fiscal receipt on the device (FDStartFiscRcp's
    DocLink/DocLinkDT/FiskMem) -- only possible if that original sale was
    itself fiscally printed. Only meaningful for a CASH original sale, same
    boundary as print_fiscal_receipt. Use `attempt_print_refund` from a
    view, never this directly."""
    from .models import SaleAttributes

    if not settings.FISCAL_ENABLED:
        raise FiscalPrintError('Fiscal printing is disabled (FISCAL_ENABLED).')

    original_sale = refund.original_sale
    if original_sale.fiscal_status != SaleAttributes.FISCAL_PRINTED or not original_sale.fiscal_receipt_number:
        raise FiscalPrintError('The original sale was never fiscally printed -- cannot storno it on the device.')

    reason_map = _storno_reason_map()
    if refund.reason not in reason_map:
        raise FiscalPrintError(f'Unknown refund reason "{refund.reason}".')

    refund_items = _validate_refund_items(refund)
    unic_sale_num = _next_unic_sale_num()
    items = [
        {'name': i.original_item.sale_item.name, 'tax_letter': _tax_letter_for(i.original_item.sale_item),
         'price': i.price_at_refund, 'qty': i.refund_quantity}
        for i in refund_items
    ]
    start_data = {
        'Operator': int(settings.FISCAL_OPERATOR_NUM),
        'Password': int(settings.FISCAL_OPERATOR_PASSWORD),
        'UnicSaleNum': unic_sale_num,
        'Invoice': '', 'Refund': 'R',
        'Reason': reason_map[refund.reason],
        'DocLink': original_sale.fiscal_receipt_number,
        # Guide's format spec (and its worked example, "26-06-19 13:41:00")
        # both include seconds -- confirmed live (2026-09-28) that leaving
        # them off ("28-09-26 09:13") made FDStartFiscRcp fail outright.
        'DocLinkDT': original_sale.fiscal_printed_at.strftime('%d-%m-%y %H:%M:%S'),
        'FiskMem': settings.FISCAL_DEVICE_FM_NUMBER,
        # Credit/InvLink deliberately NOT sent at all -- they're for a
        # credit-note document, a different operation from a plain storno
        # (see the guide's separate Credit-only field table). The guide's
        # one full worked Refund="R" example doesn't include either key,
        # even blank. A normal sale tolerates them present-but-empty fine
        # (see print_fiscal_receipt's start_data), so the two together
        # alongside Refund="R" -- still service error 7 even after fixing
        # Reason/DocLinkDT above -- is the remaining suspect, not yet
        # confirmed live either way.
    }
    _run_commands(_receipt_commands(start_data, items, 'Refund', refund.total_amount))
    return unic_sale_num


def attempt_print_refund(refund):
    """Never raises -- same pattern as attempt_print, for RefundAttributes."""
    from .models import SaleAttributes

    if settings.FISCAL_DEBUG:
        logger.info('Fiscal: attempting storno print for Refund #%s on %s', refund.pk, settings.FISCAL_COM_PORT)
    try:
        unic_sale_num = print_fiscal_refund(refund)
    except FiscalPrintError as exc:
        refund.fiscal_status = SaleAttributes.FISCAL_FAILED
        refund.fiscal_error = str(exc)
        refund.save(update_fields=['fiscal_status', 'fiscal_error'])
        logger.warning('Fiscal storno failed for Refund #%s: %s', refund.pk, exc)
    else:
        refund.fiscal_status = SaleAttributes.FISCAL_PRINTED
        refund.fiscal_error = ''
        refund.fiscal_unic_sale_num = unic_sale_num
        refund.fiscal_printed_at = timezone.now()
        refund.save(update_fields=['fiscal_status', 'fiscal_error', 'fiscal_unic_sale_num', 'fiscal_printed_at'])
        if settings.FISCAL_DEBUG:
            logger.info('Fiscal: Refund #%s storno printed -- УНП %s', refund.pk, unic_sale_num)


# Daily report Item values, straight from the FDDailyRpt table in
# kasov_aparat_ECRCommApp_Guide.pdf (order they're listed in, 0-indexed --
# confirmed by the worked example there, which used Item=0 for the Z report
# it showed a real response for).
_DAILY_RPT_ITEM_Z = 0
_DAILY_RPT_ITEM_X = 1


def _print_daily_report(item):
    """Shared by print_x_report/print_z_report -- these are on-demand admin
    actions (a button on a screen), not tied to any STORA record, so unlike
    attempt_print() this DOES raise FiscalPrintError -- the caller (the view)
    shows it directly, there's nothing to persist a retry-able status on."""
    if not settings.FISCAL_ENABLED:
        raise FiscalPrintError('Fiscal printing is disabled (FISCAL_ENABLED).')
    # Option='' -- "No operator clear" (confirmed the actual, working
    # request in the guide's own worked example uses this, not the
    # separately-illustrated "Operator clear" string); a single-till
    # pilot store has no separate per-operator totals worth resetting.
    _run_commands([{'cmd': 'FDDailyRpt', 'data': {'Item': item, 'Option': ''}}])


def print_x_report():
    """X report -- a snapshot reading of today's totals so far, does NOT
    reset anything. Safe to run any number of times a day."""
    _print_daily_report(_DAILY_RPT_ITEM_X)


def print_z_report():
    """Z report -- daily financial close. Resets today's accumulated totals
    on the device once printed; this is a real, once-a-day bookkeeping
    action, not just a reading (the device itself also enforces this: two Z
    reports the same day without a sale in between just reprint an empty
    one, per the guide)."""
    _print_daily_report(_DAILY_RPT_ITEM_Z)


def print_period_report(start_date, end_date):
    """Detailed fiscal-memory report for a date range (start_date/end_date
    are date objects) -- covers "monthly"/"yearly" reports, there's no
    separate command for those, just a wider date range here. Prints
    directly on the device's own paper; PAY=True also adds a by-payment-
    method breakdown. Nothing comes back as structured data to show in
    STORA itself."""
    if not settings.FISCAL_ENABLED:
        raise FiscalPrintError('Fiscal printing is disabled (FISCAL_ENABLED).')
    _run_commands([{'cmd': 'FDRptFromFMByDate', 'data': {
        'StartDate': start_date.strftime('%d%m%y'),
        'EndDate': end_date.strftime('%d%m%y'),
        'PAY': 'PAY',
    }}])
