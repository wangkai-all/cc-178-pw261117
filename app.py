"""Local HTTP server + JSON API for the incremental roll-up page.

Standard library only, single file, no build step: run ``python3 app.py`` and
open the printed URL.  Every mutation goes through :class:`core.service.Service`
so the transaction and idempotency rules cannot be bypassed by the browser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core import scenarios
from core.service import BatchError, Service

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "IncrementRollup/1.0"
    service: Service

    def log_message(self, fmt, *args):  # quieter, but still visible
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    # -- helpers ----------------------------------------------------
    def _send(self, status: int, body, content_type="application/json; charset=utf-8") -> None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, code: str, message: str, detail=None) -> None:
        self._send(status, {"error": {"code": code, "message": message, "detail": detail or {}}})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 5_000_000:
            raise BatchError("PAYLOAD_TOO_LARGE", "请求体过大", 413)
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BatchError("INVALID_JSON", f"请求体不是合法 JSON: {exc}", 400)

    # -- routes -----------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path, query)
            return self._static(path)
        except BatchError as exc:
            self._error(exc.status, exc.code, exc.message, exc.detail)
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, "INTERNAL", f"服务器内部错误: {exc}")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/batches":
                return self._send(200, self.service.submit_batch(body))
            if parsed.path == "/api/reset":
                return self._reset()
            self._error(404, "NOT_FOUND", f"没有这个接口: {parsed.path}")
        except BatchError as exc:
            self._error(exc.status, exc.code, exc.message, exc.detail)
        except Exception as exc:  # pragma: no cover - defensive
            self._error(500, "INTERNAL", f"服务器内部错误: {exc}")

    def _api_get(self, path: str, query: dict) -> None:
        if path == "/api/state":
            return self._send(200, self.service.state())
        if path == "/api/scenarios":
            return self._send(200, {"scenarios": scenarios.scenarios()})
        if path == "/api/batches":
            return self._send(200, {"batches": self.service.batches()})
        match = re.fullmatch(r"/api/batches/([^/]+)/ops/(\d+)/impact", path)
        if match:
            return self._send(
                200, self.service.impact(match.group(1), int(match.group(2)))
            )
        match = re.fullmatch(r"/api/batches/([^/]+)", path)
        if match:
            return self._send(200, self.service.batch_detail(match.group(1)))
        match = re.fullmatch(r"/api/snapshots/(\d+)", path)
        if match:
            return self._send(200, self.service.snapshot(int(match.group(1))))
        if path == "/api/snapshots/latest/recompute":
            latest = self.service.latest_snapshot()["version"]
            if latest is None:
                return self._error(404, "NOT_FOUND", "还没有任何快照")
            return self._send(200, self.service.recompute_from_snapshot(latest))
        match = re.fullmatch(r"/api/snapshots/(\d+)/recompute", path)
        if match:
            return self._send(200, self.service.recompute_from_snapshot(int(match.group(1))))
        self._error(404, "NOT_FOUND", f"没有这个接口: {path}")

    def _reset(self) -> None:
        """Start over with the seeded sample data (a fresh local database)."""
        service_class = type(self)
        old_service = service_class.service
        # Hold the service lock so a request already inside a transaction
        # finishes before its database file is replaced.
        with old_service.lock:
            target = old_service.db_path
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(target + suffix)
                except FileNotFoundError:
                    pass
            new_service = Service(target)
            new_service.ensure_seed()
            # Bind on the handler class so every subsequent request sees it.
            service_class.service = new_service
        self._send(200, {"status": "reset", "state": new_service.state()})

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        safe = os.path.normpath(path.lstrip("/"))
        if safe.startswith("..") or os.path.isabs(safe):
            return self._error(403, "FORBIDDEN", "路径不合法")
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(full):
            return self._error(404, "NOT_FOUND", f"文件不存在: {path}")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as handle:
            data = handle.read()
        return self._send(
            200, data, CONTENT_TYPES.get(ext, "application/octet-stream")
        )


def build_server(host: str = "127.0.0.1", port: int = 8765, db_path: str | None = None) -> ThreadingHTTPServer:
    service = Service(db_path)
    service.ensure_seed()
    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="增量区域销售汇总本地页面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "8765")))
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    httpd = build_server(args.host, args.port, args.db)
    host, port = httpd.server_address[:2]
    print(f"增量汇总页面: http://{host}:{port}/")
    print(f"数据库: {httpd.RequestHandlerClass.service.db_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
