"""Tests for the fiscal bridge. Plain unittest, no Django or database -- run
with `python manage.py test fiscal_bridge` or `python -m unittest
fiscal_bridge.tests`. No real printer: a tiny fake ECRCommApp HTTP server
stands in for it."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

from fiscal_bridge import bridge, ecr_runner

ORIGIN = 'https://stora.example.com'


def _serve(handler_class):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeEcrHandler(BaseHTTPRequestHandler):
    """Answers like ECRCommApp: echoes the command back in CmdData, or a
    device error for the command named in `fail_cmd`."""
    fail_cmd = None
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeEcrHandler.received.append(body)
        request = body['WebSrvCmd']['Cmd']['COMPortMsgList'][0]['ReqCommand']
        failed = request['Cmd'] == FakeEcrHandler.fail_cmd
        response = {'WebSrvCmd': {'HasErr': False, 'Cmd': {'COMPortMsgList': [{
            'HasErr': False,
            'ResCommand': {'IsValid': True, 'ErrorCode': 5 if failed else 0, 'CmdData': {'Echo': request['Cmd']}},
        }]}}}
        data = json.dumps(response).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class PostCommandTests(unittest.TestCase):
    def setUp(self):
        FakeEcrHandler.fail_cmd = None
        FakeEcrHandler.received = []
        self.server = _serve(FakeEcrHandler)
        self.config = ecr_runner.DeviceConfig(
            api_url=f'http://127.0.0.1:{self.server.server_port}/Api', com_port='COM7', ecrcommapp_path='x',
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_success_returns_cmd_data_and_sends_the_com_port(self):
        result = ecr_runner.post_command(self.config, 'FDStatus', {})
        self.assertEqual(result, {'Echo': 'FDStatus'})
        self.assertEqual(FakeEcrHandler.received[0]['WebSrvCmd']['Cmd']['ComPortName'], 'COM7')

    def test_device_error_code_raises_with_port_in_message(self):
        FakeEcrHandler.fail_cmd = 'FDSaleItem'
        with self.assertRaisesRegex(ecr_runner.FiscalPrintError, 'FDSaleItem on COM7: device error code 5'):
            ecr_runner.post_command(self.config, 'FDSaleItem', {})

    def test_unreachable_service_raises(self):
        config = ecr_runner.DeviceConfig(api_url='http://127.0.0.1:1/Api', com_port='COM7', ecrcommapp_path='x')
        with self.assertRaisesRegex(ecr_runner.FiscalPrintError, 'could not reach'):
            ecr_runner.post_command(config, 'FDStatus', {}, timeout=2)


class RunCommandsTests(unittest.TestCase):
    RECEIPT = [
        {'cmd': 'FDStartFiscRcp', 'data': {}},
        {'cmd': 'FDSaleItem', 'data': {}},
        {'cmd': 'FDTotalSum', 'data': {}},
        {'cmd': 'FDEndFiscRcp', 'data': {'last': True}},
    ]

    def _run(self, commands, fail_on=None):
        sent = []
        stop = MagicMock()

        def post(cmd, data):
            sent.append(cmd)
            if cmd == fail_on:
                raise ecr_runner.FiscalPrintError('boom')
            return {'cmd': cmd}

        try:
            results = ecr_runner.run_commands(commands, post=post, start=lambda: 'process', stop=stop)
        except ecr_runner.FiscalPrintError:
            results = None
        stop.assert_called_once_with('process')
        return sent, results

    def test_happy_path_returns_every_result_in_order(self):
        sent, results = self._run(self.RECEIPT)
        self.assertEqual(sent, ['FDStartFiscRcp', 'FDSaleItem', 'FDTotalSum', 'FDEndFiscRcp'])
        self.assertEqual(results[-1], {'cmd': 'FDEndFiscRcp'})

    def test_failure_inside_open_receipt_cancels_it(self):
        sent, results = self._run(self.RECEIPT, fail_on='FDSaleItem')
        self.assertIsNone(results)
        self.assertEqual(sent, ['FDStartFiscRcp', 'FDSaleItem', 'FDCancelRcp'])

    def test_failure_opening_the_receipt_does_not_cancel(self):
        sent, _ = self._run(self.RECEIPT, fail_on='FDStartFiscRcp')
        self.assertEqual(sent, ['FDStartFiscRcp'])

    def test_report_failure_does_not_cancel(self):
        sent, _ = self._run([{'cmd': 'FDDailyRpt', 'data': {}}], fail_on='FDDailyRpt')
        self.assertEqual(sent, ['FDDailyRpt'])


class BridgeHttpTests(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.job_error = None

        def fake_run_job(device, commands):
            self.jobs.append(commands)
            if self.job_error:
                raise ecr_runner.FiscalPrintError(self.job_error)
            return [{'FiscReceipt': 12}]

        settings = {
            'listen_port': 0,
            'allowed_origins': [ORIGIN],
            'device': ecr_runner.DeviceConfig(api_url='', com_port='COM4', ecrcommapp_path=''),
        }
        self.server = _serve(bridge.make_handler(settings, run_job=fake_run_job))
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _request(self, method, path, body=None, origin=ORIGIN, content_type='application/json'):
        headers = {}
        if origin:
            headers['Origin'] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = content_type
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, response.headers, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, exc.headers, json.loads(raw) if raw else None

    def test_preflight_allows_stora_and_private_network(self):
        status, headers, _ = self._request('OPTIONS', '/run')
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertEqual(headers['Access-Control-Allow-Private-Network'], 'true')

    def test_preflight_rejects_other_sites(self):
        status, _, _ = self._request('OPTIONS', '/run', origin='https://evil.example.com')
        self.assertEqual(status, 403)

    def test_run_prints_and_returns_results(self):
        commands = [{'cmd': 'FDStartFiscRcp', 'data': {}}]
        status, headers, body = self._request('POST', '/run', {'commands': commands})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True, 'results': [{'FiscReceipt': 12}]})
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertEqual(self.jobs, [commands])

    def test_run_from_another_site_is_refused_without_printing(self):
        status, _, body = self._request(
            'POST', '/run', {'commands': [{'cmd': 'FDDailyRpt', 'data': {}}]}, origin='https://evil.example.com',
        )
        self.assertEqual(status, 403)
        self.assertFalse(body['ok'])
        self.assertEqual(self.jobs, [])

    def test_run_without_origin_is_refused(self):
        status, _, _ = self._request('POST', '/run', {'commands': [{'cmd': 'FDDailyRpt', 'data': {}}]}, origin=None)
        self.assertEqual(status, 403)
        self.assertEqual(self.jobs, [])

    def test_non_json_body_is_refused(self):
        status, _, _ = self._request(
            'POST', '/run', {'commands': [{'cmd': 'FDDailyRpt', 'data': {}}]}, content_type='text/plain',
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.jobs, [])

    def test_malformed_commands_are_refused(self):
        status, _, body = self._request('POST', '/run', {'commands': [{'cmd': 'FDDailyRpt'}]})
        self.assertEqual(status, 400)
        self.assertIn('data', body['error'])

    def test_device_failure_comes_back_as_ok_false(self):
        self.job_error = 'FDSaleItem on COM4: device error code 5'
        status, _, body = self._request('POST', '/run', {'commands': [{'cmd': 'FDSaleItem', 'data': {}}]})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': False, 'error': 'FDSaleItem on COM4: device error code 5'})

    def test_status(self):
        status, _, body = self._request('GET', '/status')
        self.assertEqual(status, 200)
        self.assertEqual(body['com_port'], 'COM4')


class LoadSettingsTests(unittest.TestCase):
    def test_reads_ini(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bridge.ini')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('[bridge]\nallowed_origins = https://a.example.com/, https://b.example.com\n'
                        'com_port = COM7\nlisten_port = 7788\n')
            settings = bridge.load_settings(path)
        self.assertEqual(settings['allowed_origins'], ['https://a.example.com', 'https://b.example.com'])
        self.assertEqual(settings['listen_port'], 7788)
        self.assertEqual(settings['device'].com_port, 'COM7')

    def test_missing_allowed_origins_stops(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bridge.ini')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('[bridge]\ncom_port = COM7\n')
            with self.assertRaises(SystemExit):
                bridge.load_settings(path)
