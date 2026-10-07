"""STORA fiscal bridge -- runs on the till PC when STORA itself runs in the
cloud.

The cloud server can't reach a fiscal printer plugged into a shop PC, so
the cashier's browser does the hand-off: after a sale is saved, STORA sends
the browser the list of ECRCommApp commands for that receipt; the browser
POSTs that list here (http://127.0.0.1:<port>/run); this bridge runs it
through ecr_runner (same code a local STORA install uses) and answers with
the result, which the browser then reports back to the cloud.

Only Python's standard library -- no pip install on the till PC. Settings
come from bridge.ini next to this file (copy bridge.ini.example).

Run: python bridge.py
"""
import configparser
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Lets `python bridge.py` work from inside the fiscal_bridge folder, with
# no STORA checkout around it -- only this folder is copied to a till PC.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fiscal_bridge import ecr_runner  # noqa: E402

logger = logging.getLogger('fiscal_bridge')

BRIDGE_VERSION = 1
MAX_BODY_BYTES = 256 * 1024

# One fiscal printer, one COM port -- two receipts at once would fight
# over it, so jobs are run strictly one after another.
_device_lock = threading.Lock()


def load_settings(path):
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding='utf-8'):
        raise SystemExit(f'Settings file not found: {path} (copy bridge.ini.example to bridge.ini)')
    section = parser['bridge']
    allowed = [o.strip().rstrip('/') for o in section.get('allowed_origins', '').split(',') if o.strip()]
    if not allowed:
        raise SystemExit('bridge.ini: allowed_origins is empty -- set it to the STORA address, e.g. https://stora.example.com')
    return {
        'listen_port': section.getint('listen_port', 7777),
        'allowed_origins': allowed,
        'device': ecr_runner.DeviceConfig(
            api_url=section.get('ecr_api_url', 'http://127.0.0.1:7000/Api'),
            com_port=section.get('com_port', 'COM4'),
            ecrcommapp_path=section.get('ecrcommapp_path', ''),
            debug=section.getboolean('debug', False),
        ),
    }


def validate_commands(payload):
    """The body must be {"commands": [{"cmd": str, "data": dict}, ...]}.
    Returns the list or raises ValueError."""
    if not isinstance(payload, dict):
        raise ValueError('Body must be a JSON object.')
    commands = payload.get('commands')
    if not isinstance(commands, list) or not commands:
        raise ValueError('"commands" must be a non-empty list.')
    for command in commands:
        if not isinstance(command, dict) or not isinstance(command.get('cmd'), str) \
                or not isinstance(command.get('data'), dict):
            raise ValueError('Every command needs a "cmd" string and a "data" object.')
    return commands


def make_handler(settings, run_job=ecr_runner.run_job):
    """run_job is a parameter only so tests can swap in a fake printer."""

    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = f'STORAFiscalBridge/{BRIDGE_VERSION}'

        def _origin(self):
            return (self.headers.get('Origin') or '').rstrip('/')

        def _origin_allowed(self):
            return self._origin() in settings['allowed_origins']

        def _send_json(self, status, body):
            data = json.dumps(body).encode('utf-8')
            self.send_response(status)
            if self._origin_allowed():
                self.send_header('Access-Control-Allow-Origin', self._origin())
                self.send_header('Vary', 'Origin')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            # CORS preflight. Chrome also asks before a public https page
            # may talk to localhost at all (Private Network Access) -- that
            # is what the Allow-Private-Network header answers.
            if not self._origin_allowed():
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', self._origin())
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Access-Control-Max-Age', '600')
            self.send_header('Vary', 'Origin')
            self.end_headers()

        def do_GET(self):
            if self.path != '/status':
                self._send_json(404, {'ok': False, 'error': 'Not found.'})
                return
            self._send_json(200, {
                'ok': True, 'version': BRIDGE_VERSION, 'com_port': settings['device'].com_port,
            })

        def do_POST(self):
            if self.path != '/run':
                self._send_json(404, {'ok': False, 'error': 'Not found.'})
                return
            # Any web page the cashier happens to open could try to POST
            # here. Browsers always send the page's real Origin and pages
            # can't fake it, so only STORA's own address gets through.
            if not self._origin_allowed():
                self._send_json(403, {'ok': False, 'error': f'Origin not allowed: {self._origin() or "(none)"}'})
                return
            # JSON only -- a JSON body forces the browser's CORS preflight
            # above, a plain form post would skip it.
            if not (self.headers.get('Content-Type') or '').startswith('application/json'):
                self._send_json(415, {'ok': False, 'error': 'Content-Type must be application/json.'})
                return
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {'ok': False, 'error': 'Missing or too large body.'})
                return
            try:
                commands = validate_commands(json.loads(self.rfile.read(length).decode('utf-8')))
            except ValueError as exc:  # json.JSONDecodeError is a ValueError too
                self._send_json(400, {'ok': False, 'error': str(exc)})
                return

            with _device_lock:
                try:
                    results = run_job(settings['device'], commands)
                except ecr_runner.FiscalPrintError as exc:
                    logger.warning('Fiscal job failed: %s', exc)
                    # 200, not 5xx -- the bridge itself worked fine, it's
                    # the device that said no; the browser reads the body.
                    self._send_json(200, {'ok': False, 'error': str(exc)})
                    return
            self._send_json(200, {'ok': True, 'results': results})

        def log_message(self, fmt, *args):
            logger.info('%s %s', self.address_string(), fmt % args)

    return BridgeHandler


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    here = os.path.dirname(os.path.abspath(__file__))
    settings = load_settings(os.path.join(here, 'bridge.ini'))
    # 127.0.0.1 only -- never reachable from other machines on the network.
    server = ThreadingHTTPServer(('127.0.0.1', settings['listen_port']), make_handler(settings))
    logger.info(
        'STORA fiscal bridge listening on http://127.0.0.1:%s for %s (printer on %s)',
        settings['listen_port'], ', '.join(settings['allowed_origins']), settings['device'].com_port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
