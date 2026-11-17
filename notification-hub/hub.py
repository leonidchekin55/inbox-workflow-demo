"""Loopback-only receiver and viewer for minimal phone notification metadata."""
from argparse import ArgumentParser
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from automation.daily_alerts import DailyAlertQueue, EventConflictError

IDENTIFIER = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
CLIENT_ID = re.compile(r'^[0-9a-f]{64}$')
MAX_BODY = 4096


def create_settings(directory, devices, grouping, timezone_name):
    directory = Path(directory).expanduser().resolve()
    if grouping not in ('all', 'channel', 'client'):
        raise ValueError('Некорректный режим группировки.')
    try:
        ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError('Некорректный часовой пояс.') from error
    if len(set(devices)) != len(devices):
        raise ValueError('Укажите неповторяющиеся ID телефонов.')
    if any(not IDENTIFIER.fullmatch(device) or len(device) > 64 for device in devices):
        raise ValueError('ID телефона должен содержать до 64 латинских букв, цифр или . _ : -')
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    tokens = {device: secrets.token_urlsafe(32) for device in devices}
    settings = {'grouping': grouping, 'timezone': timezone_name}
    for name, data in (('settings.json', settings), ('device-tokens.json', tokens)):
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
    queue = DailyAlertQueue(directory / 'alerts.sqlite', grouping=grouping, timezone_name=timezone_name)
    os.chmod(directory / 'alerts.sqlite', 0o600)
    return queue


def read_settings(directory):
    with (Path(directory) / 'settings.json').open(encoding='utf-8') as stream:
        settings = json.load(stream)
    if (not isinstance(settings, dict) or set(settings) != {'grouping', 'timezone'}
            or settings['grouping'] not in ('all', 'channel', 'client')
            or not isinstance(settings['timezone'], str)):
        raise ValueError('Некорректные настройки очереди.')
    try:
        ZoneInfo(settings['timezone'])
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError('Некорректный часовой пояс.') from error
    with (Path(directory) / 'device-tokens.json').open(encoding='utf-8') as stream:
        tokens = json.load(stream)
    if not isinstance(tokens, dict):
        raise ValueError('Некорректные настройки телефонов.')
    for device, token in tokens.items():
        if (not IDENTIFIER.fullmatch(device) or len(device) > 64 or not isinstance(token, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token)):
            raise ValueError('Некорректные настройки телефона.')
    settings['devices'] = {device: sha256(token.encode()).hexdigest() for device, token in tokens.items()}
    return settings


def add_device(directory, device):
    """Create one device credential, serializing concurrent registrations."""
    directory = Path(directory).expanduser().resolve()
    if not IDENTIFIER.fullmatch(device) or len(device) > 64:
        raise ValueError('Некорректный ID телефона.')
    read_settings(directory)
    path = directory / 'device-tokens.json'
    database_path = directory / 'alerts.sqlite'
    if not database_path.is_file():
        raise FileNotFoundError('База уведомлений не найдена.')
    with sqlite3.connect(database_path, timeout=30) as database:
        database.execute('BEGIN EXCLUSIVE')
        with path.open(encoding='utf-8') as stream:
            tokens = json.load(stream)
        if device in tokens:
            raise ValueError('Такой телефон уже зарегистрирован.')
        tokens[device] = secrets.token_urlsafe(32)
        fd, temporary = tempfile.mkstemp(prefix='.device-tokens-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(tokens, stream, indent=2, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def validate_event(value, device):
    required = {'event_id', 'platform', 'account_id', 'kind', 'occurred_at', 'client_id'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('invalid fields')
    for field in ('event_id', 'platform', 'account_id'):
        if not isinstance(value[field], str) or not IDENTIFIER.fullmatch(value[field]):
            raise ValueError('invalid identifier')
    if value['account_id'] != device:
        raise ValueError('invalid device')
    if value['kind'] not in ('message', 'call'):
        raise ValueError('invalid kind')
    if value['client_id'] is not None and (not isinstance(value['client_id'], str) or not CLIENT_ID.fullmatch(value['client_id'])):
        raise ValueError('invalid client')
    if not isinstance(value['occurred_at'], str) or len(value['occurred_at']) > 40:
        raise ValueError('invalid timestamp')
    try:
        instant = datetime.fromisoformat(value['occurred_at'].replace('Z', '+00:00'))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError('missing timezone')
    except ValueError as error:
        raise ValueError('invalid timestamp') from error
    return {**value, 'occurred_at': instant}


def _reply(handler, status, payload, content_type='application/json; charset=utf-8'):
    data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Referrer-Policy', 'no-referrer')
    handler.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
    handler.end_headers()
    handler.wfile.write(data)


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)


class DeviceHandler(QuietHandler):
    def do_POST(self):
        if self.path != '/v1/events':
            return _reply(self, 404, {'error': 'not found'})
        auth = self.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else ''
        digest = sha256(token.encode('utf-8')).hexdigest()
        try:
            devices = read_settings(self.server.directory)['devices']
        except (OSError, ValueError, json.JSONDecodeError):
            self.close_connection = True
            return _reply(self, 503, {'error': 'temporarily unavailable'})
        device = next((name for name, expected in devices.items()
                       if token and hmac.compare_digest(digest, expected)), None)
        if not device:
            self.close_connection = True
            return _reply(self, 401, {'error': 'unauthorized'})
        length_values = self.headers.get_all('Content-Length', [])
        if (len(length_values) != 1 or len(length_values[0]) > 6 or not length_values[0].isdigit() or
                self.headers.get('Transfer-Encoding') or
                self.headers.get_content_type() != 'application/json'):
            self.close_connection = True
            return _reply(self, 400, {'error': 'invalid request'})
        length = int(length_values[0])
        if length < 1 or length > MAX_BODY:
            self.close_connection = True
            return _reply(self, 413, {'error': 'invalid size'})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete body')
            event = validate_event(json.loads(raw), device)
            alert_id, created = self.server.queue.enqueue(**event)
        except EventConflictError:
            return _reply(self, 409, {'error': 'event conflict'})
        except (ValueError, UnicodeError, TypeError, json.JSONDecodeError):
            return _reply(self, 400, {'error': 'invalid event'})
        except sqlite3.Error:
            return _reply(self, 503, {'error': 'temporarily unavailable'})
        return _reply(self, 201 if created else 200, {'accepted': True})


class DashboardHandler(QuietHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        allowed = {f'localhost:{port}', f'127.0.0.1:{port}'}
        if self.headers.get('Host', '') not in allowed:
            return _reply(self, 403, {'error': 'invalid host'})
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{host}' for host in allowed}:
            return _reply(self, 403, {'error': 'invalid origin'})
        if self.path == '/api/alerts':
            try:
                with sqlite3.connect(f'{Path(self.server.database).as_uri()}?mode=rw', uri=True) as db:
                    db.row_factory = sqlite3.Row
                    has_email_attempts = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_attempts'").fetchone()
                    if has_email_attempts:
                        rows = db.execute(
                            'SELECT a.id, a.platform, a.account_id, a.kind, a.occurred_at, '
                            'a.status, e.state AS mail_state FROM daily_alerts a '
                            'LEFT JOIN email_attempts e ON a.id = e.alert_id ORDER BY a.id DESC LIMIT 100'
                        ).fetchall()
                    else:
                        rows = db.execute(
                            'SELECT id, platform, account_id, kind, occurred_at, status, '
                            'NULL AS mail_state FROM daily_alerts ORDER BY id DESC LIMIT 100'
                        ).fetchall()
            except sqlite3.Error:
                return _reply(self, 503, {'error': 'temporarily unavailable'})
            return _reply(self, 200, {'alerts': [dict(row) for row in rows]})
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path not in files:
            return _reply(self, 404, {'error': 'not found'})
        name, kind = files[self.path]
        return _reply(self, 200, (Path(__file__).parent / name).read_bytes(), kind)


def build_servers(directory, device_port=8765, dashboard_port=8766):
    directory = Path(directory).expanduser().resolve()
    settings = read_settings(directory)
    database = directory / 'alerts.sqlite'
    if not database.is_file():
        raise FileNotFoundError('База уведомлений не найдена. Восстановите её из резервной копии.')
    queue = DailyAlertQueue(database, grouping=settings['grouping'], timezone_name=settings['timezone'])
    device = ThreadingHTTPServer(('127.0.0.1', device_port), DeviceHandler)
    try:
        dashboard = ThreadingHTTPServer(('127.0.0.1', dashboard_port), DashboardHandler)
    except Exception:
        device.server_close()
        raise
    device.queue = queue
    device.directory = directory
    dashboard.database = database
    return device, dashboard


def main():
    parser = ArgumentParser(description='Локальная очередь уведомлений')
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('init')
    setup.add_argument('--data-dir', required=True, help='новая папка для закрытых настроек и базы')
    setup.add_argument('--device', default=[], action='append', help='ID телефона, если уже известен')
    setup.add_argument('--grouping', choices=('all', 'channel', 'client'), default='client')
    setup.add_argument('--timezone', default='Europe/Moscow')
    serve = commands.add_parser('serve')
    serve.add_argument('--data-dir', required=True)
    serve.add_argument('--device-port', type=int, default=8765)
    serve.add_argument('--dashboard-port', type=int, default=8766)
    register = commands.add_parser('add-device')
    register.add_argument('--data-dir', required=True)
    register.add_argument('--device', required=True, help='ID установки, указанный на телефоне')
    args = parser.parse_args()
    if args.command == 'init':
        create_settings(args.data_dir, args.device, args.grouping, args.timezone)
        print(f'Настройки созданы в {args.data_dir}. Ключи телефонов находятся в закрытом файле device-tokens.json.')
        return
    if args.command == 'add-device':
        add_device(args.data_dir, args.device)
        print('Телефон добавлен. Ключ находится в закрытом файле device-tokens.json.')
        return
    device, dashboard = build_servers(args.data_dir, args.device_port, args.dashboard_port)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (device, dashboard)]
    for thread in threads:
        thread.start()
    print(f'Панель: http://127.0.0.1:{dashboard.server_address[1]}/')
    print(f'Приёмник на 127.0.0.1:{device.server_address[1]}. Для телефона нужен отдельно настроенный защищённый HTTPS-канал.')
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in (device, dashboard):
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'Не удалось запустить программу: {error}', file=sys.stderr)
        sys.exit(1)
