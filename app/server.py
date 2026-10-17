"""HTTP layer (stdlib only). Routing, auth, transactions, idempotency keys."""
import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, service as s

STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "index.html")
# (method, pattern, handler, auth_required, is_write)
ROUTES = [
    ("POST", r"/api/login", lambda c, ctx, m, q, b: s.login(c, b), False, True),
    ("GET", r"/api/me", lambda c, ctx, m, q, b: s.me(c, ctx, q), True, False),
    ("GET", r"/api/dashboard", lambda c, ctx, m, q, b: s.dashboard(c, ctx, q), True, False),
    ("GET", r"/api/notifications", lambda c, ctx, m, q, b: s.my_notifications(c, ctx, q), True, False),
    ("GET", r"/api/teams/(\d+)/members", lambda c, ctx, m, q, b: s.team_members(c, ctx, int(m[0])), True, False),
    ("GET", r"/api/items", lambda c, ctx, m, q, b: s.list_items(c, ctx, q), True, False),
    ("POST", r"/api/items", lambda c, ctx, m, q, b: s.create_item(c, ctx, b), True, True),
    ("GET", r"/api/items/(\d+)", lambda c, ctx, m, q, b: s.get_item(c, ctx, int(m[0])), True, False),
    ("PATCH", r"/api/items/(\d+)", lambda c, ctx, m, q, b: s.update_item(c, ctx, int(m[0]), b), True, True),
    ("POST", r"/api/items/(\d+)/claim", lambda c, ctx, m, q, b: s.claim_item(c, ctx, int(m[0]), b), True, True),
    ("POST", r"/api/items/(\d+)/transition", lambda c, ctx, m, q, b: s.transition_item(c, ctx, int(m[0]), b), True, True),
    ("POST", r"/api/items/(\d+)/comments", lambda c, ctx, m, q, b: s.add_comment(c, ctx, int(m[0]), b), True, True),
    ("GET", r"/api/items/(\d+)/activity", lambda c, ctx, m, q, b: s.get_activity(c, ctx, int(m[0]), q), True, False),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test/demo output clean
        pass

    def _send(self, status, payload, headers=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if not isinstance(payload, bytes) else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, code, message, extra=None):
        self._send(status, {"error": {"code": code, "message": message}, **(extra or {})})

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PATCH(self): self._dispatch("PATCH")

    def _dispatch(self, method):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if method == "GET" and not url.path.startswith("/api"):
            with open(STATIC, "rb") as f:
                return self._send(200, f.read())
        route, match = None, None
        for r in ROUTES:
            if r[0] == method and (m := re.fullmatch(r[1], url.path)):
                route, match = r, m.groups()
                break
        if route is None:
            return self._error(404, "not_found", "No such route")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._error(400, "bad_json", "Body must be a JSON object")
        conn = db.connect()
        try:
            ctx = None
            if route[3]:
                auth = self.headers.get("Authorization", "")
                ctx = s.authenticate(conn, auth[7:] if auth.startswith("Bearer ") else None)
                if ctx is None:
                    return self._error(401, "unauthorized", "Login required")
            if not route[4]:
                status, payload = route[2](conn, ctx, match, q, body)
                return self._send(status, payload)
            self._write(conn, ctx, route, match, q, body, method, url.path, raw)
        except s.ApiError as e:
            self._error(e.status, e.code, e.message, e.extra)
        except Exception as e:  # noqa: BLE001
            __import__("traceback").print_exc(); self._error(500, "internal", f"{type(e).__name__}")
        finally:
            conn.close()

    def _write(self, conn, ctx, route, match, q, body, method, path, raw):
        """One transaction per write: business change + activity + outbox + idempotency record."""
        key = self.headers.get("Idempotency-Key")
        fingerprint = hashlib.sha256(f"{method} {path} {raw.decode()}".encode()).hexdigest()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if key and ctx:
                if len(key) > 255:
                    raise s.ApiError(400, "validation", "Idempotency-Key too long")
                row = conn.execute("SELECT * FROM idempotency_keys WHERE key=? AND user_id=?", (key, ctx.uid)).fetchone()
                if row:
                    conn.execute("ROLLBACK")
                    if row["fingerprint"] != fingerprint:
                        return self._error(422, "idempotency_key_reuse", "Key was used with a different request")
                    return self._send_stored(row)
            status, payload = route[2](conn, ctx, match, q, body)
            if key and ctx:
                conn.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?)",
                             (key, ctx.uid, fingerprint, status, json.dumps(payload), db.now()))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        self._send(status, payload)

    def _send_stored(self, row):
        raw = row["response"].encode()
        self.send_response(row["status_code"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Idempotent-Replay", "true")
        self.end_headers()
        self.wfile.write(raw)


def make_server(host="127.0.0.1", port=8000):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv
