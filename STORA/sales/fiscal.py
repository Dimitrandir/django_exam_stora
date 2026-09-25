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
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class FiscalPrintError(Exception):
    """Any failure talking to the fiscal device -- message is safe to store
    in SaleAttributes.fiscal_error and show to a cashier/manager."""


def _post_command(cmd, cmd_data, timeout=15):
    """POSTs one ReqCommand to ECRCommApp and returns the ResCommand's
    CmdData dict on success. Raises FiscalPrintError on any transport,
    service, or device-reported error."""
    body = {
        'WebSrvCmd': {
            'CmdType': 'CmdCOMPort',
            'Cmd': {
                'ComPortName': settings.FISCAL_COM_PORT,
                'COMPortMsgList': [{'ReqCommand': {'Cmd': cmd, 'CmdData': cmd_data}}],
            },
        },
    }
    request = urllib.request.Request(
        settings.FISCAL_API_URL, data=json.dumps(body).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FiscalPrintError(f'Could not reach the fiscal device service: {exc}') from exc

    web_srv_cmd = result.get('WebSrvCmd', {})
    if web_srv_cmd.get('HasErr'):
        raise FiscalPrintError(f'Fiscal device service error: {web_srv_cmd.get("Res")}')

    msg_list = web_srv_cmd.get('Cmd', {}).get('COMPortMsgList') or []
    if not msg_list:
        raise FiscalPrintError('Fiscal device service returned an empty response.')
    msg = msg_list[0]
    if msg.get('HasErr'):
        raise FiscalPrintError(f'Fiscal device COM error: {msg.get("Res")}')

    res_command = msg.get('ResCommand', {})
    if not res_command.get('IsValid') or res_command.get('ErrorCode', 0) != 0:
        raise FiscalPrintError(f'{cmd} failed -- device error code {res_command.get("ErrorCode")}')
    return res_command.get('CmdData', {})


def _wait_for_server(process, timeout=10):
    """Polls with a real (harmless, read-only) FDStatus call -- confirms
    both the HTTP service AND the COM port are actually reachable, not just
    that the process started."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise FiscalPrintError('ECRCommApp exited before it became ready.')
        try:
            _post_command('FDStatus', {}, timeout=2)
            return
        except FiscalPrintError:
            time.sleep(0.5)
    raise FiscalPrintError('ECRCommApp did not become ready in time.')


def _start_ecrcommapp():
    if not settings.FISCAL_ECRCOMMAPP_PATH:
        raise FiscalPrintError('FISCAL_ECRCOMMAPP_PATH is not configured.')
    try:
        process = subprocess.Popen([settings.FISCAL_ECRCOMMAPP_PATH])
    except OSError as exc:
        raise FiscalPrintError(f'Could not start ECRCommApp: {exc}') from exc
    _wait_for_server(process)
    return process


def _stop_ecrcommapp(process):
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        logger.warning('ECRCommApp (pid %s) did not exit cleanly -- killing it', process.pid)
        try:
            process.kill()
        except Exception:
            logger.exception('Could not kill ECRCommApp process %s', process.pid)


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


def _run_receipt(start_cmd_data, items, sale_type, amount_in):
    """Shared open -> sell -> total -> close sequence for both a real sale
    and a storno -- only what differs between them (FDStartFiscRcp's extra
    fields, whether each line is a Sale or Refund, the paid/refunded amount)
    is passed in. `items` is a list of {'name', 'tax_letter', 'price', 'qty'}
    dicts. Returns FDEndFiscRcp's response CmdData (has FiscReceipt/
    AllReceipt)."""
    process = _start_ecrcommapp()
    receipt_open = False
    try:
        _post_command('FDStartFiscRcp', start_cmd_data)
        receipt_open = True

        for item in items:
            _post_command('FDSaleItem', {
                'Text1': item['name'][:28],
                'Text2': '',
                'TaxGrp': item['tax_letter'],
                'Sale type': sale_type,
                'Price': str(item['price']),
                'Qty': str(item['qty']),
                'Percent': '0',
                'Netto': '0',
            })

        _post_command('FDTotalSum', {
            'Text1': '', 'Text2': '',
            'Payment type': 'В Брой',
            'AmountIn': str(amount_in),
        })

        end_result = _post_command('FDEndFiscRcp', {})
        receipt_open = False
    except FiscalPrintError:
        if receipt_open:
            # Best-effort only -- FDCancelRcp's exact parameters were never
            # exercised live (unlike every other command here), so this may
            # itself fail; that's logged, not raised, since we're already
            # inside a failure path and must not mask the original error.
            try:
                _post_command('FDCancelRcp', {})
            except FiscalPrintError:
                logger.warning('Could not cancel a half-open fiscal receipt.')
        raise
    finally:
        _stop_ecrcommapp(process)

    return end_result


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
    end_result = _run_receipt(start_data, items, 'Sale', sale.amount_paid)
    return {'unic_sale_num': unic_sale_num, 'receipt_number': end_result.get('FiscReceipt')}


def attempt_print(sale):
    """Never raises -- safe to call right after a sale is saved. Records the
    outcome on the sale itself (fiscal_status/fiscal_error/...) instead."""
    from .models import SaleAttributes

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


# Storno Reason values, confirmed live against the real ECRWebApp dropdown
# (0/1/2, in this exact order) -- matches RefundAttributes.REASON_CHOICES'
# own order 1:1 by design (see that model's docstring), spelled out
# explicitly here anyway rather than relying on list position, so a future
# reordering of REASON_CHOICES can't silently send the wrong Reason to the
# device.
def _storno_reason_map():
    from .models import RefundAttributes
    return {
        RefundAttributes.RETURN_COMPLAINT: 0,
        RefundAttributes.OPERATOR_ERROR: 1,
        RefundAttributes.TAX_BASE_REDUCTION: 2,
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
        'Invoice': '', 'Refund': 'R', 'Credit': '',
        'Reason': reason_map[refund.reason],
        'DocLink': original_sale.fiscal_receipt_number,
        'DocLinkDT': original_sale.fiscal_printed_at.strftime('%d-%m-%y %H:%M'),
        'FiskMem': settings.FISCAL_DEVICE_FM_NUMBER,
        'InvLink': '',
    }
    _run_receipt(start_data, items, 'Refund', refund.total_amount)
    return unic_sale_num


def attempt_print_refund(refund):
    """Never raises -- same pattern as attempt_print, for RefundAttributes."""
    from .models import SaleAttributes

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
    process = _start_ecrcommapp()
    try:
        # Option='' -- "No operator clear" (confirmed the actual, working
        # request in the guide's own worked example uses this, not the
        # separately-illustrated "Operator clear" string); a single-till
        # pilot store has no separate per-operator totals worth resetting.
        _post_command('FDDailyRpt', {'Item': item, 'Option': ''})
    finally:
        _stop_ecrcommapp(process)


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
    process = _start_ecrcommapp()
    try:
        _post_command('FDRptFromFMByDate', {
            'StartDate': start_date.strftime('%d%m%y'),
            'EndDate': end_date.strftime('%d%m%y'),
            'PAY': 'PAY',
        })
    finally:
        _stop_ecrcommapp(process)
