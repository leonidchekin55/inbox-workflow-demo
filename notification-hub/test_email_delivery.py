from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import shutil
import smtplib
import socketserver
import sqlite3
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_delivery import EmailDelivery, EmailSettings
from hub import build_servers, create_settings
from automation.daily_alerts import DailyAlertQueue


class FakeSMTP:
    instances = []
    fail_at = None

    def __init__(self, *args, **kwargs):
        if self.fail_at == 'connect':
            raise OSError('connect failed')
        self.messages = []
        self.logged_in = False
        FakeSMTP.instances.append(self)

    def ehlo_or_helo_if_needed(self):
        pass

    def starttls(self, *, context):
        self.context = context

    def ehlo(self):
        pass

    def login(self, username, password):
        self.logged_in = True

    def send_message(self, message, *, from_addr, to_addrs):
        self.messages.append(message)
        self.envelope = (from_addr, to_addrs)
        if self.fail_at == 'send':
            raise smtplib.SMTPServerDisconnected('lost response')
        return {}

    def quit(self):
        pass

    def close(self):
        pass


class LocalSMTPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        stream = sock.makefile('rb')
        sock.sendall(b'220 localhost ESMTP\r\n')
        data = []
        while True:
            line = stream.readline()
            if not line:
                break
            command = line.split(b' ', 1)[0].strip().upper()
            if command in (b'EHLO', b'HELO'):
                sock.sendall(b'250-localhost\r\n250 STARTTLS\r\n' if not isinstance(sock, ssl.SSLSocket) else b'250 localhost\r\n')
            elif command == b'STARTTLS':
                sock.sendall(b'220 Ready to start TLS\r\n')
                stream.close()
                sock = self.server.context.wrap_socket(sock, server_side=True)
                stream = sock.makefile('rb')
            elif command in (b'MAIL', b'RCPT'):
                sock.sendall(b'250 OK\r\n')
            elif command == b'DATA':
                sock.sendall(b'354 End with dot\r\n')
                while True:
                    chunk = stream.readline()
                    if not chunk or chunk == b'.\r\n':
                        break
                    data.append(chunk)
                self.server.message = b''.join(data)
                sock.sendall(b'250 accepted\r\n')
            elif command == b'QUIT':
                sock.sendall(b'221 bye\r\n')
                break
            else:
                sock.sendall(b'500 unsupported\r\n')
        stream.close()
        sock.close()


class EmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = DailyAlertQueue(Path(self.temp.name) / 'alerts.db', grouping='client', timezone_name='Europe/Moscow')
        self.settings = EmailSettings('smtp.example.test', 587, 'sender@example.test', 'recipient@example.test', 'user', 'secret')
        self.delivery = EmailDelivery(self.queue, self.settings, smtp_class=FakeSMTP, smtp_ssl_class=FakeSMTP)
        self.now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        FakeSMTP.instances = []
        FakeSMTP.fail_at = None
        self.alert, _ = self.queue.enqueue(platform='Telegram', account_id='private-account', event_id='private-event',
                                           kind='call', occurred_at=self.now, client_id='private-client')

    def test_acceptance_sends_only_minimal_event_metadata(self):
        self.assertEqual(self.delivery.run_one(now=self.now), 'accepted')
        self.assertEqual(self.queue.get(self.alert)['status'], 'delivered')
        message = FakeSMTP.instances[0].messages[0]
        self.assertTrue(FakeSMTP.instances[0].logged_in)
        self.assertIn('звонок', message.get_content())
        self.assertIn('Telegram', message.get_content())
        self.assertNotIn('private-', message.as_string())
        self.assertNotIn('secret', message.as_string())
        self.assertEqual(FakeSMTP.instances[0].envelope, ('sender@example.test', ['recipient@example.test']))
        self.assertEqual(self.delivery.run_one(now=self.now), 'empty')

    def test_connection_failure_can_retry_and_does_not_block_other_alerts(self):
        FakeSMTP.fail_at = 'connect'
        self.assertEqual(self.delivery.run_one(now=self.now), 'retryable')
        self.assertEqual(self.delivery.run_one(now=self.now), 'empty')
        FakeSMTP.fail_at = None
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(minutes=5)), 'accepted')

    def test_unknown_result_requires_human_resolution(self):
        FakeSMTP.fail_at = 'send'
        self.assertEqual(self.delivery.run_one(now=self.now), 'uncertain')
        self.assertEqual(self.delivery.review()[0]['alert_id'], self.alert)
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(days=1)), 'empty')
        self.assertTrue(self.delivery.resolve(self.alert, retry=True, now=self.now + timedelta(days=1)))
        FakeSMTP.fail_at = None
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(days=1)), 'accepted')
        self.assertEqual(self.delivery.review(), [])

    def test_interrupted_attempt_is_not_resent_without_review(self):
        self.queue.claim_next(now=self.now, lease_seconds=1)
        self.set_attempt('started')
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(seconds=2)), 'uncertain')
        self.assertEqual(len(FakeSMTP.instances), 0)
        self.assertTrue(self.delivery.resolve(self.alert, retry=False, now=self.now + timedelta(seconds=3)))
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(seconds=3)), 'accepted')
        self.assertEqual(len(FakeSMTP.instances), 0)

    def test_recovery_after_acceptance_does_not_resend(self):
        self.queue.claim_next(now=self.now, lease_seconds=1)
        self.set_attempt('accepted')
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(seconds=2)), 'accepted')
        self.assertEqual(self.queue.get(self.alert)['status'], 'delivered')
        self.assertEqual(len(FakeSMTP.instances), 0)

    def test_review_can_resolve_an_expired_interrupted_attempt_without_restarting_worker(self):
        self.queue.claim_next(now=self.now, lease_seconds=10)
        self.set_attempt('started')
        self.assertFalse(self.delivery.resolve(self.alert, retry=True, now=self.now + timedelta(seconds=9)))
        self.assertTrue(self.delivery.resolve(self.alert, retry=True, now=self.now + timedelta(seconds=10)))
        self.assertEqual(self.delivery.run_one(now=self.now + timedelta(seconds=10)), 'accepted')

    def test_configuration_requires_tls_and_consistent_credentials(self):
        with self.assertRaises(ValueError):
            EmailSettings('smtp.example.test', 25, 'x@example.test', 'y@example.test', security='plain')
        with self.assertRaises(ValueError):
            EmailSettings('smtp.example.test', 587, 'x@example.test', 'y@example.test', username='user')
        with self.assertRaises(ValueError):
            EmailSettings('smtp.example.test', 587, 'x@example.test\nBcc: x', 'y@example.test')
        with self.assertRaises(ValueError):
            EmailSettings('smtp.example.test', 587, 'x@example.test', 'y@example.test, z@example.test')
        self.assertNotIn('secret', repr(self.settings))

    def set_attempt(self, state):
        with sqlite3.connect(self.queue.database) as conn:
            conn.execute('INSERT INTO email_attempts (alert_id, state, updated_at) VALUES (?, ?, ?)',
                         (self.alert, state, self.now.isoformat()))

    def test_lock_prevents_concurrent_send_and_review(self):
        with self.delivery._exclusive() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.delivery.run_one(now=self.now), 'busy')
            self.assertFalse(self.delivery.resolve(self.alert, retry=True, now=self.now))
        self.assertEqual(self.delivery.run_one(now=self.now), 'accepted')

    @unittest.skipUnless(shutil.which('openssl'), 'openssl is needed to generate a local test certificate')
    def test_real_smtp_starttls_with_local_server(self):
        folder = Path(self.temp.name)
        key, cert, config = folder / 'key.pem', folder / 'cert.pem', folder / 'openssl.cnf'
        config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=localhost\n[v3]\nsubjectAltName=DNS:localhost\nbasicConstraints=CA:TRUE\n')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(cert), '-config', str(config)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        with socketserver.TCPServer(('127.0.0.1', 0), LocalSMTPHandler) as server:
            server.context = context
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                settings = EmailSettings('localhost', server.server_address[1], 'sender@example.test', 'recipient@example.test')
                delivery = EmailDelivery(self.queue, settings)
                test_context = ssl.create_default_context(cafile=str(cert))
                with patch('email_delivery.ssl.create_default_context', return_value=test_context):
                    self.assertEqual(delivery.run_one(now=self.now), 'accepted')
                self.assertIn(b'Content-Type: text/plain', server.message)
                self.assertNotIn(b'private-client', server.message)
            finally:
                server.shutdown()
                thread.join()

    def test_hub_event_reaches_email_sender(self):
        directory = Path(self.temp.name) / 'hub'
        create_settings(directory, ['phone1'], 'client', 'Europe/Moscow')
        tokens = json.loads((directory / 'device-tokens.json').read_text())
        receiver, dashboard = build_servers(directory, device_port=0, dashboard_port=0)
        thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        thread.start()
        event = {'event_id': 'event1', 'platform': 'org.telegram.messenger', 'account_id': 'phone1',
                 'kind': 'message', 'occurred_at': self.now.isoformat(), 'client_id': 'a' * 64}
        request = Request(f'http://127.0.0.1:{receiver.server_address[1]}/v1/events',
                          data=json.dumps(event).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + tokens['phone1']},
                          method='POST')
        try:
            with urlopen(request) as response:
                self.assertEqual(response.status, 201)
            delivery = EmailDelivery(receiver.queue, self.settings, smtp_class=FakeSMTP, smtp_ssl_class=FakeSMTP)
            self.assertEqual(delivery.run_one(now=self.now), 'accepted')
            self.assertIn('Telegram', FakeSMTP.instances[-1].messages[0].get_content())
        finally:
            receiver.shutdown()
            thread.join()
            receiver.server_close()
            dashboard.server_close()


if __name__ == '__main__':
    unittest.main()
