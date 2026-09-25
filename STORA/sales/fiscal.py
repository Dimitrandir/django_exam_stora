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


def print_fiscal_receipt(sale):
    """Prints a real fiscal receipt for `sale` on the Daisy Perfect S01.
    Raises FiscalPrintError on any failure. Only meaningful for CASH sales
    right now (see sales_add) -- CARD/MIXED have no fiscal Payment type
    mapped yet. Callers that must not let a failure here block anything
    (i.e. every real caller) should use `attempt_print` instead."""
    if not settings.FISCAL_ENABLED:
        raise FiscalPrintError('Fiscal printing is disabled (FISCAL_ENABLED).')

    items = _validate_items(sale)
    unic_sale_num = _next_unic_sale_num()

    process = _start_ecrcommapp()
    receipt_open = False
    try:
        _post_command('FDStartFiscRcp', {
            'Operator': int(settings.FISCAL_OPERATOR_NUM),
            'Password': int(settings.FISCAL_OPERATOR_PASSWORD),
            'UnicSaleNum': unic_sale_num,
            'Invoice': '', 'Refund': '', 'Credit': '',
            'Reason': 0, 'DocLink': 0, 'DocLinkDT': '', 'FiskMem': '', 'InvLink': '',
        })
        receipt_open = True

        for item in items:
            _post_command('FDSaleItem', {
                'Text1': item.sale_item.name[:28],
                'Text2': '',
                'TaxGrp': _tax_letter_for(item.sale_item),
                'Sale type': 'Sale',
                'Price': str(item.price_at_sale),
                'Qty': str(item.sale_quantity),
                'Percent': '0',
                'Netto': '0',
            })

        _post_command('FDTotalSum', {
            'Text1': '', 'Text2': '',
            'Payment type': 'В Брой',
            'AmountIn': str(sale.amount_paid),
        })

        _post_command('FDEndFiscRcp', {})
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
                logger.warning('Could not cancel the half-open fiscal receipt for Sale #%s', sale.pk)
        raise
    finally:
        _stop_ecrcommapp(process)

    return unic_sale_num


def attempt_print(sale):
    """Never raises -- safe to call right after a sale is saved. Records the
    outcome on the sale itself (fiscal_status/fiscal_error/...) instead."""
    from .models import SaleAttributes

    try:
        unic_sale_num = print_fiscal_receipt(sale)
    except FiscalPrintError as exc:
        sale.fiscal_status = SaleAttributes.FISCAL_FAILED
        sale.fiscal_error = str(exc)
        sale.save(update_fields=['fiscal_status', 'fiscal_error'])
        logger.warning('Fiscal print failed for Sale #%s: %s', sale.pk, exc)
    else:
        sale.fiscal_status = SaleAttributes.FISCAL_PRINTED
        sale.fiscal_error = ''
        sale.fiscal_unic_sale_num = unic_sale_num
        sale.fiscal_printed_at = timezone.now()
        sale.save(update_fields=['fiscal_status', 'fiscal_error', 'fiscal_unic_sale_num', 'fiscal_printed_at'])
