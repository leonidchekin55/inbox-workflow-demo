"""Contract, isolation, and browser-origin checks for the synthetic demo."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
import threading
import unittest

from automation.server import LocalServer, MAX_BODY_BYTES
from automation.store import DemoStore, VersionConflictError

ASSETS = {
    "/": (b'<meta name="csrf-token" content="__CSRF_TOKEN__">', "text/html; charset=utf-8"),
    "/app.js": (b"'use strict';", "text/javascript; charset=utf-8"),
    "/styles.css": (b"body {}", "text/css; charset=utf-8"),
}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer(0, ASSETS)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.origin = f"http://{self.host}"
        self.csrf = self.server.csrf_token

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, *, method="GET", host=None, origin=None, token=None,
                content_type="application/json", body=None, extra_headers=()):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", self.host if host is None else host)
        if origin is not None:
            conn.putheader("Origin", origin)
        if token is not None:
            conn.putheader("X-CSRF-Token", token)
        if method == "POST":
            conn.putheader("Content-Type", content_type)
            data = b"{}" if body is None else body
            conn.putheader("Content-Length", str(len(data)))
        else:
            data = None
        for key, value in extra_headers:
            conn.putheader(key, value)
        conn.endheaders(data)
        response = conn.getresponse()
        payload = response.read()
        headers = dict(response.getheaders())
        code = response.status
        conn.close()
        return code, headers, payload

    def post(self, path, data, **overrides):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return self.request(path, method="POST", origin=overrides.pop("origin", self.origin),
                            token=overrides.pop("token", self.csrf), body=body, **overrides)

    def test_loopback_assets_and_state_are_memory_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        code, headers, page = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(self.csrf.encode(), page)
        self.assertNotIn(b"__CSRF_TOKEN__", page)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/styles.css")[0], 200)
        self.assertIn(self.csrf.encode(), self.request("/panel/")[2])
        self.assertEqual(self.request("/panel/app.js")[0], 200)
        self.assertEqual(self.request("/panel/styles.css")[0], 200)
        self.assertEqual(self.request("/../automation/store.py")[0], 404)
        code, _, data = self.request("/api/state")
        self.assertEqual(code, 200)
        state = json.loads(data)
        self.assertEqual(state["messages"], [])
        self.assertTrue(state["templates"])

    def test_explicit_load_then_edit_draft_and_status(self):
        code, _, data = self.post("/api/demo/load", {})
        self.assertEqual(code, 200)
        state = json.loads(data)
        self.assertEqual({m["platform"] for m in state["messages"]}, {"WhatsApp", "Telegram", "MAX"})
        self.assertEqual({m["account_id"] for m in state["messages"]}, {"1", "2", "3"})
        self.assertTrue(all(m["chat_id"].startswith("demo-chat-") for m in state["messages"]))
        version = state["messages"][0]["version"]
        code, _, data = self.post("/api/messages/demo-1/draft", {"text": "Учебный черновик", "version": version})
        self.assertEqual(code, 200)
        updated = json.loads(data)["messages"][0]
        self.assertEqual(updated["draft"], "Учебный черновик")
        self.assertGreater(updated["version"], version)
        code, _, data = self.post("/api/messages/demo-1/status", {"status": "done", "version": updated["version"]})
        self.assertEqual(code, 200)
        updated = json.loads(data)["messages"][0]
        self.assertEqual(updated["status"], "done")
        self.assertEqual(self.post("/api/messages/demo-1/draft", {"text": "", "version": updated["version"]})[0], 200)
        self.assertEqual(self.post("/api/demo/load", {"unexpected": True})[0], 400)
        self.assertEqual(self.post("/api/demo/load", {})[0], 200)
        self.assertEqual(json.loads(self.request("/api/state")[2])["messages"][0]["draft"], "")

    def test_host_origin_csrf_content_type_and_body_limits(self):
        self.assertEqual(self.request("/api/state", host="evil.example")[0], 400)
        self.assertEqual(self.request("/api/state", host="localhost:" + str(self.server.server_port))[0], 400)
        self.assertEqual(self.request("/api/state", extra_headers=[("Host", self.host)])[0], 400)
        self.assertEqual(self.post("/api/demo/load", {}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.post("/api/demo/load", {}, origin="null")[0], 403)
        self.assertEqual(self.post("/api/demo/load", {}, token="wrong")[0], 403)
        self.assertEqual(self.post("/api/demo/load", {}, token="ÿ")[0], 403)
        self.assertEqual(self.request("/api/demo/load", method="POST", token=self.csrf)[0], 403)
        self.assertEqual(self.request("/api/demo/load", method="POST", origin=self.origin)[0], 403)
        self.assertEqual(self.post("/api/demo/load", {}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("/api/demo/load", method="POST", origin=self.origin, token=self.csrf,
                                      body=b"x" * (MAX_BODY_BYTES + 1))[0], 413)
        self.assertEqual(json.loads(self.request("/api/state")[2])["messages"], [])

    def test_validation_and_no_messenger_endpoints(self):
        state = json.loads(self.post("/api/demo/load", {})[2])
        version = state["messages"][0]["version"]
        path = "/api/messages/demo-1/draft"
        self.assertEqual(self.post(path, {"text": "x" * 2001, "version": version})[0], 400)
        self.assertEqual(self.post(path, {"text": "a\x00b", "version": version})[0], 400)
        self.assertEqual(self.post(path, {"text": "ok", "version": version, "extra": True})[0], 400)
        self.assertEqual(self.post(path, {"text": 3, "version": version})[0], 400)
        self.assertEqual(self.post(path, {"text": "ok"})[0], 400)
        self.assertEqual(self.post(path, {"text": "ok", "version": True})[0], 400)
        self.assertEqual(self.post(path, {"text": "ok", "version": 0})[0], 400)
        self.assertEqual(self.post("/api/messages/demo-1/status", {"status": "sent", "version": version})[0], 400)
        self.assertEqual(self.post("/api/messages/unknown/status", {"status": "done", "version": version})[0], 404)
        self.assertEqual(self.post("/api/messages/demo-1/send", {})[0], 404)
        self.assertEqual(self.post("/webhook/telegram", {})[0], 404)
        self.assertEqual(self.request(path, method="POST", origin=self.origin, token=self.csrf,
                                      body=b'{"text":"x","text":"y"}')[0], 400)
        self.assertEqual(self.request(path, method="POST", origin=self.origin, token=self.csrf,
                                      body=b"\xff")[0], 400)
        self.assertEqual(json.loads(self.request("/api/state")[2])["messages"][0]["draft"], "")

    def test_stale_tab_cannot_overwrite_saved_draft_or_status(self):
        state = json.loads(self.post("/api/demo/load", {})[2])
        version = state["messages"][0]["version"]
        path = "/api/messages/demo-1/draft"
        first = self.post(path, {"text": "Учебный вариант A", "version": version})
        self.assertEqual(first[0], 200)
        self.assertEqual(self.post(path, {"text": "Учебный вариант B", "version": version})[0], 409)
        self.assertEqual(self.post("/api/messages/demo-1/status", {"status": "done", "version": version})[0], 409)
        current = json.loads(self.request("/api/state")[2])["messages"][0]
        self.assertEqual(current["draft"], "Учебный вариант A")
        self.assertEqual(current["status"], "new")
        # Reload reuses the display ID, but must not reuse the original revision.
        self.post("/api/demo/load", {})
        self.assertEqual(self.post(path, {"text": "Устаревший текст", "version": version})[0], 409)
        self.assertEqual(json.loads(self.request("/api/state")[2])["messages"][0]["draft"], "")


class StoreTests(unittest.TestCase):
    def test_packaged_panel_has_expected_token_and_assets(self):
        server = LocalServer(0)
        try:
            page = server.assets["/"][0]
            self.assertEqual(page.count(b"__CSRF_TOKEN__"), 1)
            self.assertTrue(b'src="/app.js"' in page or b'src="/panel/app.js"' in page)
            self.assertTrue(b'href="/styles.css"' in page or b'href="/panel/styles.css"' in page)
            self.assertGreater(len(server.assets["/app.js"][0]), 0)
            self.assertGreater(len(server.assets["/styles.css"][0]), 0)
        finally:
            server.server_close()

    def test_same_event_is_deduplicated_within_account_only(self):
        store = DemoStore()
        payload = ("Telegram", "1", "demo-event-44", "demo-chat-8", "Учебный пример: запрос")
        created, state = store.ingest_demo_event(*payload)
        self.assertTrue(created)
        self.assertEqual(len(state["messages"]), 1)
        created, state = store.ingest_demo_event(*payload)
        self.assertFalse(created)
        self.assertEqual(len(state["messages"]), 1)
        created, state = store.ingest_demo_event("Telegram", "2", *payload[2:])
        self.assertTrue(created)
        self.assertEqual(len(state["messages"]), 2)
        created, state = store.ingest_demo_event("MAX", "1", *payload[2:])
        self.assertTrue(created)
        self.assertEqual(len(state["messages"]), 3)

    def test_concurrent_duplicate_only_inserts_once(self):
        store = DemoStore()
        payload = ("MAX", "3", "demo-event-22", "demo-chat-3", "Учебный пример: текст")
        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(lambda _: store.ingest_demo_event(*payload)[0], range(24)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(store.state()["messages"]), 1)

    def test_concurrent_updates_compare_and_modify_under_one_lock(self):
        store = DemoStore()
        version = store.load_demo()["messages"][0]["version"]

        def update(i):
            try:
                store.update("demo-1", "draft", f"Учебный вариант {i}", version)
                return True
            except VersionConflictError:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(update, range(24)))
        self.assertEqual(sum(outcomes), 1)
        self.assertGreater(store.state()["messages"][0]["version"], version)

    def test_only_fictional_internal_events_are_accepted(self):
        store = DemoStore()
        with self.assertRaises(ValueError):
            store.ingest_demo_event("Telegram", "1", "real-event-1", "demo-chat-1", "Учебный пример: тест")
        with self.assertRaises(ValueError):
            store.ingest_demo_event("Telegram", "1", "demo-event-1", "real-chat-1", "Учебный пример: тест")
        with self.assertRaises(ValueError):
            store.ingest_demo_event("Telegram", "1", "demo-event-1", "demo-chat-1", "обычное сообщение")
        self.assertEqual(store.state()["messages"], [])


if __name__ == "__main__":
    unittest.main()
