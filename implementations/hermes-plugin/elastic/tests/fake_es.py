"""A tiny in-process fake of the Elasticsearch _bulk API (stdlib only), for exporter tests.

Implements just enough: POST /_bulk with NDJSON `create` actions, 201 / 409 per item, auth
check, and switches to simulate an outage (connection refused / 503), throttling (per-item
429) and mapping rejections (per-item 400).
"""

from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeES:
    def __init__(self, auth: str = "Basic aGVybWVzX2V4cG9ydGVyOnRlc3Q=", certfile=None, keyfile=None):
        self.docs = {}           # (index, _id) -> doc
        self.requests = []       # raw bulk bodies
        self.headers = []        # request headers (to check auth / content type)
        self.auth = auth
        self.fail_http = 0       # next N requests answer this many 503s
        self.throttle_ids = {}   # _id -> remaining 429 answers
        self.reject_ids = set()  # _id -> always 400 mapper_parsing_exception
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                with fake.lock:
                    fake.headers.append(dict(self.headers))
                    if self.headers.get("Authorization") != fake.auth:
                        return self._send(401, {"error": "unauthorized"})
                    if fake.fail_http:
                        fake.fail_http -= 1
                        return self._send(503, {"error": "unavailable"})
                    if self.path != "/_bulk":
                        return self._send(404, {"error": "no handler"})
                    fake.requests.append(raw)
                    items, errors = fake._bulk(raw)
                    return self._send(200, {"took": 1, "errors": errors, "items": items})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.scheme = "http"
        if certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            self.scheme = "https"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                       daemon=True)

    @property
    def url(self):
        return f"{self.scheme}://127.0.0.1:{self.server.server_address[1]}"

    def _bulk(self, raw: bytes):
        assert raw.endswith(b"\n"), "bulk body must end with a newline"
        lines = raw.decode("utf-8").split("\n")[:-1]
        assert len(lines) % 2 == 0, "bulk body must be action/source pairs"
        items, errors = [], False
        for i in range(0, len(lines), 2):
            action = json.loads(lines[i])
            doc = json.loads(lines[i + 1])
            (op, meta), = action.items()
            assert op == "create", op
            key = (meta["_index"], meta["_id"])
            if meta["_id"] in self.reject_ids:
                errors = True
                items.append({op: {**meta, "status": 400, "error": {
                    "type": "document_parsing_exception", "reason": "failed to parse field [x]"}}})
            elif self.throttle_ids.get(meta["_id"], 0) > 0:
                self.throttle_ids[meta["_id"]] -= 1
                errors = True
                items.append({op: {**meta, "status": 429, "error": {"type": "es_rejected_execution_exception"}}})
            elif key in self.docs:
                errors = True
                items.append({op: {**meta, "status": 409, "error": {"type": "version_conflict_engine_exception"}}})
            else:
                self.docs[key] = doc
                items.append({op: {**meta, "status": 201, "result": "created"}})
        return items, errors

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

