"""Loopback-only HTTP server for a synthetic, in-memory inbox demonstration."""
from argparse import ArgumentParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets

from .store import DemoStore, VersionConflictError

MAX_BODY_BYTES = 8192
TOKEN_MARKER = b"__CSRF_TOKEN__"
ROOT = Path(__file__).resolve().parent.parent
STATIC_ASSETS = {
    "/": ("panel/index.html", "text/html; charset=utf-8"),
    "/app.js": ("panel/app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("panel/styles.css", "text/css; charset=utf-8"),
}
ASSET_ALIASES = {"/panel/": "/", "/panel/app.js": "/app.js", "/panel/styles.css": "/styles.css"}
MESSAGE_ROUTE = re.compile(r"^/api/messages/([a-z0-9-]{1,40})/(draft|status)$")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def load_assets():
    """Load only the fixed, trusted local panel assets; paths from requests are never used."""
    assets = {}
    for path, (relative, mime) in STATIC_ASSETS.items():
        data = (ROOT / relative).read_bytes()
        assets[path] = (data, mime)
    if assets["/"][0].count(TOKEN_MARKER) != 1:
        raise ValueError("The panel must contain exactly one CSRF token marker")
    return assets


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, assets=None):
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("invalid port")
        self.assets = load_assets() if assets is None else assets
        if set(self.assets) != set(STATIC_ASSETS) or self.assets["/"][0].count(TOKEN_MARKER) != 1:
            raise ValueError("invalid panel assets")
        self.store = DemoStore()
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)

    def handle_error(self, request, client_address):
        # Do not print request bodies, headers, addresses, or error tracebacks.
        return


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoInbox"
    sys_version = ""

    def log_message(self, format, *args):
        # Never log paths, IDs, headers, or message bodies.
        return

    def _header_once(self, name):
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def _host_ok(self):
        return self._header_once("Host") == f"127.0.0.1:{self.server.server_port}"

    def _reply(self, code, data, content_type="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def _json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._reply(code, data)

    def _error(self, code):
        self._json(code, {"error": HTTPStatus(code).phrase})

    def do_GET(self):
        if not self._host_ok():
            return self._error(400)
        if self.path == "/api/state":
            return self._json(200, self.server.store.state())
        asset_path = ASSET_ALIASES.get(self.path, self.path)
        if asset_path in self.server.assets:
            data, mime = self.server.assets[asset_path]
            if asset_path == "/":
                data = data.replace(TOKEN_MARKER, self.server.csrf_token.encode("ascii"))
            return self._reply(200, data, mime)
        return self._error(404)

    def do_POST(self):
        if not self._host_ok():
            return self._error(400)
        if self._header_once("Origin") != f"http://127.0.0.1:{self.server.server_port}":
            return self._error(403)
        token = self._header_once("X-CSRF-Token")
        if token is None or not token.isascii() or not secrets.compare_digest(token, self.server.csrf_token):
            return self._error(403)
        ctype = self._header_once("Content-Type")
        if ctype is None or ctype.split(";", 1)[0].strip().lower() != "application/json":
            return self._error(415)
        if self.headers.get_all("Transfer-Encoding", []):
            return self._error(400)
        length = self._header_once("Content-Length")
        if length is None or not re.fullmatch(r"[0-9]{1,8}", length):
            return self._error(400)
        size = int(length)
        if size > MAX_BODY_BYTES:
            return self._error(413)
        try:
            self.connection.settimeout(5)
            body = self.rfile.read(size)
            if len(body) != size:
                return self._error(400)
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        except (TimeoutError, OSError, UnicodeDecodeError, ValueError, RecursionError):
            return self._error(400)
        if not isinstance(data, dict):
            return self._error(400)
        if self.path == "/api/demo/load":
            if data:
                return self._error(400)
            return self._json(200, self.server.store.load_demo())
        match = MESSAGE_ROUTE.fullmatch(self.path)
        if not match:
            return self._error(404)
        message_id, action = match.groups()
        field = "text" if action == "draft" else "status"
        if set(data) != {field, "version"}:
            return self._error(400)
        try:
            return self._json(200, self.server.store.update(message_id, action, data[field], data["version"]))
        except VersionConflictError:
            return self._error(409)
        except ValueError:
            return self._error(400)
        except KeyError:
            return self._error(404)

    def do_OPTIONS(self):
        return self._error(405 if self._host_ok() else 400)

    do_PUT = do_OPTIONS
    do_PATCH = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_HEAD = do_OPTIONS


def main():
    parser = ArgumentParser(description="Локальная демонстрация: только вымышленные данные.")
    parser.add_argument("--port", type=int, default=8787, help="порт на 127.0.0.1 (по умолчанию 8787)")
    args = parser.parse_args()
    try:
        server = LocalServer(args.port)
    except (OSError, ValueError):
        parser.exit(1, "Не удалось запустить локальную демонстрацию. Проверьте порт и файлы панели.\n")
    print(f"Демонстрация доступна: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
