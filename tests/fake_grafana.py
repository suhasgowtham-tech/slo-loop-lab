"""A tiny stand-in for Grafana's HTTP API (and Prometheus' /api/v1/query) for the marker and dashboard-check tests."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGrafana:
    def __init__(self, dashboards: set[str] | None = None, rules: set[str] | None = None, delay_s: float = 0.0, fail_posts: int = 0,
                 login_ok: bool = True):
        self.dashboards = set(dashboards or set())
        self.rules = set(rules or set())
        self.delay_s, self.fail_posts, self.login_ok = delay_s, fail_posts, login_ok
        self.requests: list[dict] = []
        self.annotations: dict[int, dict] = {}
        self._next = 1
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, code: int, body: dict | None = None):
                raw = json.dumps(body or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _record(self, body=None):
                outer.requests.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if outer.delay_s:
                    time.sleep(outer.delay_s)

            def do_GET(self):
                self._record()
                if self.path == "/api/health":
                    return self._send(200, {"database": "ok", "version": "12.0.0"})
                if self.path.startswith("/api/dashboards/uid/"):
                    if not outer.login_ok:
                        return self._send(401, {"message": "Invalid username or password"})
                    uid = self.path.rsplit("/", 1)[1]
                    return self._send(200 if uid in outer.dashboards else 404, {"dashboard": {"uid": uid}})
                if self.path.startswith("/api/v1/query"):
                    from urllib.parse import parse_qs, urlparse
                    q = parse_qs(urlparse(self.path).query).get("query", [""])[0]
                    name = q.split('"')[1] if '"' in q else ""
                    res = [{"metric": {}, "value": [0, "1"]}] if name in outer.rules else []
                    return self._send(200, {"status": "success", "data": {"resultType": "vector", "result": res}})
                self._send(404)

            def do_POST(self):
                body = self._body()
                self._record(body)
                if outer.fail_posts > 0:
                    outer.fail_posts -= 1
                    return self._send(500, {"message": "boom"})
                if self.path == "/api/annotations":
                    i = outer._next
                    outer._next += 1
                    outer.annotations[i] = body
                    return self._send(200, {"id": i, "message": "Annotation added"})
                self._send(404)

            def do_PUT(self):
                body = self._body()
                self._record(body)
                if self.path.startswith("/api/annotations/"):
                    outer.annotations[int(self.path.rsplit("/", 1)[1])] = body
                    return self._send(200, {"message": "Annotation updated"})
                self._send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
