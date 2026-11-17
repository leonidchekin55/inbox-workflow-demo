from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
import urllib.request

import hub
import launcher


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'private'
        hub.create_settings(self.dir, ['phone1', 'phone2'], 'client', 'Europe/Moscow')
        self.tokens = json.loads((self.dir / 'device-tokens.json').read_text())
        self.device, self.dashboard = hub.build_servers(self.dir, 0, 0)
        for server in (self.device, self.dashboard):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
        self.event = {
            'event_id': 'event-1', 'platform': 'org.example.messenger',
            'account_id': 'phone1', 'kind': 'message',
            'occurred_at': datetime.now(timezone.utc).isoformat(), 'client_id': 'a' * 64,
        }

    def request(self, server, method, path, body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_address[1], timeout=4)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, data

    def send(self, event=None, token=None, path='/v1/events', body=None):
        if body is None:
            body = json.dumps(event if event is not None else self.event).encode()
        return self.request(self.device, 'POST', path, body, {
            'Authorization': f'Bearer {token if token is not None else self.tokens["phone1"]}',
            'Content-Type': 'application/json',
        })

    def test_private_setup_and_listener_binding(self):
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        for name in ('settings.json', 'device-tokens.json', 'alerts.sqlite'):
            self.assertEqual(os.stat(self.dir / name).st_mode & 0o777, 0o600)
        self.assertNotIn(self.tokens['phone1'], (self.dir / 'settings.json').read_text())
        self.assertEqual(self.device.server_address[0], '127.0.0.1')
        self.assertEqual(self.dashboard.server_address[0], '127.0.0.1')

    def test_authentication_idempotency_and_dashboard(self):
        self.assertEqual(self.send(token='wrong')[0], 401)
        self.assertEqual(self.send()[0], 201)
        self.assertEqual(self.send()[0], 200)
        self.assertEqual(self.send({**self.event, 'kind': 'call'})[0], 409)
        self.assertEqual(self.send({**self.event, 'event_id': 'event-2', 'kind': 'call'})[0], 200)
        status, data = self.request(self.dashboard, 'GET', '/api/alerts')
        self.assertEqual(status, 200)
        alerts = json.loads(data)['alerts']
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['kind'], 'message')
        self.assertNotIn('client_id', alerts[0])
        self.assertNotIn('lease_token', alerts[0])
        self.assertEqual(self.request(self.dashboard, 'GET', '/')[0], 200)
        self.assertEqual(self.request(self.dashboard, 'GET', '/settings.json')[0], 404)

    def test_separate_credentials_and_unknown_clients(self):
        self.assertEqual(self.send({**self.event, 'account_id': 'phone2'})[0], 400)
        self.assertEqual(self.send({**self.event, 'account_id': 'phone2'}, token=self.tokens['phone2'])[0], 201)
        self.assertEqual(self.send({**self.event, 'client_id': None})[0], 201)
        self.assertEqual(self.send({**self.event, 'event_id': 'another', 'client_id': None})[0], 201)
        self.assertEqual(len(json.loads(self.request(self.dashboard, 'GET', '/api/alerts')[1])['alerts']), 3)

    def test_invalid_payloads_and_origin_rejected(self):
        self.assertEqual(self.send({**self.event, 'text': 'message content'})[0], 400)
        self.assertEqual(self.send({**self.event, 'client_id': 'person name'})[0], 400)
        self.assertEqual(self.send({**self.event, 'occurred_at': '2026-01-01T00:00:00'})[0], 400)
        self.assertEqual(self.send(body=b'{' + b' ' * 5000)[0], 413)
        self.assertEqual(self.send(path='/bad')[0], 404)
        self.assertEqual(self.request(self.dashboard, 'GET', '/api/alerts', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request(self.dashboard, 'GET', '/api/alerts', headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.request(self.device, 'GET', '/v1/events')[0], 501)
        with sqlite3.connect(self.dir / 'alerts.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM alert_events').fetchone()[0], 0)

    def test_registration_without_restart_and_concurrent_additions(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda n: hub.add_device(self.dir, f'new-{n}'), range(4)))
        tokens = json.loads((self.dir / 'device-tokens.json').read_text())
        self.assertTrue(all(f'new-{n}' in tokens for n in range(4)))
        self.assertEqual(os.stat(self.dir / 'device-tokens.json').st_mode & 0o777, 0o600)
        event = {**self.event, 'account_id': 'new-1', 'event_id': 'new-test'}
        self.assertEqual(self.send(event, token=tokens['new-1'])[0], 201)
        with self.assertRaises(ValueError):
            hub.add_device(self.dir, 'new-1')

    def test_can_initialize_before_devices_are_known(self):
        empty_dir = Path(self.tmp.name) / 'no-devices-yet'
        hub.create_settings(empty_dir, [], 'all', 'Europe/Moscow')
        self.assertEqual(hub.read_settings(empty_dir)['devices'], {})
        hub.add_device(empty_dir, 'phone-uuid')
        self.assertIn('phone-uuid', hub.read_settings(empty_dir)['devices'])

    def test_missing_database_is_not_recreated(self):
        empty_dir = Path(self.tmp.name) / 'missing-database'
        hub.create_settings(empty_dir, [], 'all', 'Europe/Moscow')
        database = empty_dir / 'alerts.sqlite'
        database.unlink()
        with self.assertRaises(FileNotFoundError):
            hub.build_servers(empty_dir, 0, 0)
        self.assertFalse(database.exists())
        with self.assertRaises(FileNotFoundError):
            hub.add_device(empty_dir, 'device-1')
        self.assertFalse(database.exists())

    def test_dashboard_exposes_uncertain_mail_state(self):
        self.assertEqual(self.send()[0], 201)
        with sqlite3.connect(self.dir / 'alerts.sqlite') as database:
            database.execute('CREATE TABLE email_attempts (alert_id INTEGER PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL)')
            database.execute("INSERT INTO email_attempts VALUES (1, 'uncertain', '2026-11-17T00:00:00+00:00')")
        status, body = self.request(self.dashboard, 'GET', '/api/alerts')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['alerts'][0]['mail_state'], 'uncertain')

    def test_bad_settings_fail_without_mutating_database_or_traceback(self):
        database = self.dir / 'alerts.sqlite'
        original = database.read_bytes()
        for broken in ('[]', '{}', '{"grouping":"nonsense","timezone":"UTC"}',
                       '{"grouping":"all","timezone":"No/Such_Zone"}', 'not-json'):
            with self.subTest(settings=broken):
                (self.dir / 'settings.json').write_text(broken)
                with self.assertRaises(ValueError):
                    hub.read_settings(self.dir)
                completed = subprocess.run(
                    [sys.executable, str(Path(hub.__file__)), 'serve', '--data-dir', str(self.dir)],
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn('Traceback', completed.stderr)
                self.assertEqual(database.read_bytes(), original)


class LauncherTests(unittest.TestCase):
    def test_first_launch_opens_ready_dashboard_and_retains_data(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / 'private data'
            opened = []

            def observe(url):
                with urllib.request.urlopen(url, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                opened.append(url)
                return True

            servers, threads, url = launcher.start(directory, device_port=0, dashboard_port=0, opener=observe)
            try:
                self.assertEqual(opened, [url])
                self.assertEqual(hub.read_settings(directory)['devices'], {})
                database = directory / 'alerts.sqlite'
                self.assertTrue(database.is_file())
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)
            before = database.read_bytes()
            servers, threads, _ = launcher.start(directory, device_port=0, dashboard_port=0, opener=observe)
            try:
                self.assertEqual(database.read_bytes(), before)
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)

    def test_incomplete_existing_directory_is_not_overwritten(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / 'private'
            directory.mkdir()
            settings = directory / 'settings.json'
            settings.write_text('{"partial":true}')
            opened = []
            with self.assertRaises(ValueError):
                launcher.start(directory, device_port=0, dashboard_port=0, opener=opened.append)
            self.assertEqual(settings.read_text(), '{"partial":true}')
            self.assertEqual(sorted(item.name for item in directory.iterdir()), ['settings.json'])
            self.assertEqual(opened, [])


if __name__ == '__main__':
    unittest.main()
