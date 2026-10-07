"""Talking to Daisy's ECRCommApp (the JSON API in front of the fiscal
printer) -- the part of the fiscal integration that has to run on the PC
the printer is plugged into.

No Django imports on purpose. Two programs use this same file:
- STORA itself, when it runs on the till PC (the local shop installs) --
  through STORA/sales/fiscal.py, which builds the commands and runs them
  here directly.
- fiscal_bridge/bridge.py, when STORA runs in the cloud and can't reach the
  printer -- the cashier's browser hands the commands built by the cloud
  STORA to the bridge on the till PC, and the bridge runs them here.

ECRCommApp is spawned fresh around each job and killed right after --
confirmed live (kasov_aparat.md) that holding the COM port open
continuously would starve RACS (the store's separate access-control
system), which also needs the same port at times. Field names/formats come
straight from kasov_aparat_ECRCommApp_Guide.pdf (repo root), verified
against the real device on 2026-09-25 -- not guessed.
"""
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# A receipt is "open" on the device between these two commands. If anything
# fails in between, the receipt must be cancelled on the device -- see
# run_commands.
OPEN_RECEIPT_CMD = 'FDStartFiscRcp'
CLOSE_RECEIPT_CMD = 'FDEndFiscRcp'
CANCEL_RECEIPT_CMD = 'FDCancelRcp'


class FiscalPrintError(Exception):
    """Any failure talking to the fiscal device -- message is safe to store
    in SaleAttributes.fiscal_error and show to a cashier/manager."""


@dataclass
class DeviceConfig:
    api_url: str
    com_port: str
    ecrcommapp_path: str
    debug: bool = False


def post_command(config, cmd, cmd_data, timeout=15):
    """POSTs one ReqCommand to ECRCommApp and returns the ResCommand's
    CmdData dict on success. Raises FiscalPrintError on any transport,
    service, or device-reported error. Every error message names the COM
    port that was used -- asked for live so a failure on screen says
    exactly what was tried and where, not just that something failed (the
    port is the one thing that's actually changed between working and
    failing sessions so far, see the COM4->COM7 drift noted in
    DEPLOYMENT.md/CLAUDE.md)."""
    port = config.com_port
    if config.debug:
        logger.info('Fiscal: sending %s on %s -- %s', cmd, port, cmd_data)
    body = {
        'WebSrvCmd': {
            'CmdType': 'CmdCOMPort',
            'Cmd': {
                'ComPortName': port,
                'COMPortMsgList': [{'ReqCommand': {'Cmd': cmd, 'CmdData': cmd_data}}],
            },
        },
    }
    request = urllib.request.Request(
        config.api_url, data=json.dumps(body).encode('utf-8'), method='POST',
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FiscalPrintError(f'{cmd} on {port}: could not reach the fiscal device service -- {exc}') from exc

    web_srv_cmd = result.get('WebSrvCmd', {})
    if web_srv_cmd.get('HasErr'):
        raise FiscalPrintError(f'{cmd} on {port}: fiscal device service error -- {web_srv_cmd.get("Res")}')

    msg_list = web_srv_cmd.get('Cmd', {}).get('COMPortMsgList') or []
    if not msg_list:
        raise FiscalPrintError(f'{cmd} on {port}: fiscal device service returned an empty response.')
    msg = msg_list[0]
    if msg.get('HasErr'):
        raise FiscalPrintError(f'{cmd} on {port}: fiscal device COM error -- {msg.get("Res")}')

    res_command = msg.get('ResCommand', {})
    if not res_command.get('IsValid') or res_command.get('ErrorCode', 0) != 0:
        raise FiscalPrintError(f'{cmd} on {port}: device error code {res_command.get("ErrorCode")}')
    if config.debug:
        logger.info('Fiscal: %s on %s succeeded -- %s', cmd, port, res_command.get('CmdData', {}))
    return res_command.get('CmdData', {})


def wait_for_server(config, process, timeout=10):
    """Polls with a real (harmless, read-only) FDStatus call -- confirms
    both the HTTP service AND the COM port are actually reachable, not just
    that the process started."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise FiscalPrintError('ECRCommApp exited before it became ready.')
        try:
            post_command(config, 'FDStatus', {}, timeout=2)
            return
        except FiscalPrintError as exc:
            last_error = exc
            time.sleep(0.5)
    raise FiscalPrintError(
        f'ECRCommApp did not become ready in time on {config.com_port} '
        f'(last attempt: {last_error}).'
    )


def start_ecrcommapp(config):
    if not config.ecrcommapp_path:
        raise FiscalPrintError(
            'ECRCommApp path is not configured (FISCAL_ECRCOMMAPP_PATH in .env, or ecrcommapp_path in bridge.ini).'
        )
    if config.debug:
        logger.info('Fiscal: starting ECRCommApp (%s) for %s', config.ecrcommapp_path, config.com_port)
    try:
        process = subprocess.Popen([config.ecrcommapp_path])
    except OSError as exc:
        raise FiscalPrintError(f'Could not start ECRCommApp ({config.ecrcommapp_path}): {exc}') from exc
    wait_for_server(config, process)
    if config.debug:
        logger.info('Fiscal: ECRCommApp is ready on %s', config.com_port)
    return process


def stop_ecrcommapp(process):
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


def run_commands(commands, post, start, stop):
    """Starts ECRCommApp, sends `commands` in order, stops it again.
    `commands` is a list of {'cmd': ..., 'data': {...}} dicts. Returns the
    CmdData of every command, in the same order. Raises FiscalPrintError on
    the first failure.

    post/start/stop are passed in rather than called directly, so
    STORA/sales/fiscal.py can hand over its own wrappers (which its tests
    mock out) -- use run_job when you just have a DeviceConfig."""
    process = start()
    receipt_open = False
    results = []
    try:
        for command in commands:
            results.append(post(command['cmd'], command['data']))
            if command['cmd'] == OPEN_RECEIPT_CMD:
                receipt_open = True
            elif command['cmd'] == CLOSE_RECEIPT_CMD:
                receipt_open = False
    except FiscalPrintError:
        if receipt_open:
            # Best-effort only -- may itself fail, which is logged rather
            # than raised, since we're already inside a failure path and
            # must not mask the original error. Confirmed live (2026-09-28,
            # a FDPrintBarcode failure mid-receipt): this genuinely prints
            # a real fiscal "АНУЛИРАН БОН" (voided receipt) with its own
            # valid signature/QR code -- the device won't just silently
            # drop a half-open fiscal receipt, it formally voids it on
            # paper. That receipt is the correct, expected outcome of a
            # failure here, not a sign that something is stuck/corrupted.
            try:
                post(CANCEL_RECEIPT_CMD, {})
            except FiscalPrintError:
                logger.warning('Could not cancel a half-open fiscal receipt.')
        raise
    finally:
        stop(process)
    return results


def run_job(config, commands):
    """run_commands against a real ECRCommApp described by `config` -- what
    the bridge uses."""
    return run_commands(
        commands,
        post=lambda cmd, data: post_command(config, cmd, data),
        start=lambda: start_ecrcommapp(config),
        stop=stop_ecrcommapp,
    )
