"""提供技术首发与受限披露服务的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from digital_trade_foundation.api import route as foundation_route
from digital_trade_foundation.errors import DomainError, ValidationError

from .service import LaunchService
from .storage import LaunchDatabase


def route(service: LaunchService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把首发披露请求分派到领域服务，其余路径回落到基础服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    if not parsed.path.startswith("/launch"):
        return foundation_route(service, method, path, body, headers)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and parsed.path == "/launch/versions":
            return _receipt(service.register_version(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/audiences":
            return _receipt(service.add_audience(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications":
            return _receipt(service.create_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications/submit":
            return _receipt(service.submit_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications/verify":
            return _receipt(service.verify_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications/countersign":
            return _receipt(service.countersign_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications/publish":
            return _receipt(service.publish_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications/withdraw":
            return _receipt(service.withdraw_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/applications/correct":
            return _receipt(service.correct_application(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/prerequisites/clear":
            return _receipt(service.clear_prerequisite(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/credentials":
            return _receipt(service.issue_credential(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/access":
            return _receipt(service.record_access(**body))
        if method == "POST" and parsed.path == "/launch/freezes":
            return _receipt(service.freeze_scope(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/freezes/lift":
            return _receipt(service.lift_freeze(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/launch/maintenance":
            return 200, service.run_maintenance()
        if method == "GET" and parsed.path == "/launch/versions":
            query = parse_qs(parsed.query)
            version_id = query.get("version_id", [None])[0]
            if version_id:
                return 200, service.get_version(actor_id, version_id)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": service.list_versions(actor_id, site_id)}
        if method == "GET" and parsed.path == "/launch/applications":
            query = parse_qs(parsed.query)
            application_id = query.get("application_id", [None])[0]
            if application_id:
                return 200, service.get_application(actor_id, application_id)
            version_id = query.get("version_id", [""])[0]
            if not version_id:
                raise ValidationError("version_id 不能为空")
            return 200, {"items": service.list_applications(actor_id, version_id)}
        if method == "GET" and parsed.path == "/launch/audiences":
            query = parse_qs(parsed.query)
            version_id = query.get("version_id", [""])[0]
            if not version_id:
                raise ValidationError("version_id 不能为空")
            return 200, {"items": service.list_audiences(actor_id, version_id)}
        if method == "GET" and parsed.path == "/launch/credentials":
            query = parse_qs(parsed.query)
            credential_id = query.get("credential_id", [""])[0]
            if not credential_id:
                raise ValidationError("credential_id 不能为空")
            return 200, service.get_credential(actor_id, credential_id)
        if method == "GET" and parsed.path == "/launch/access-events":
            query = parse_qs(parsed.query)
            return 200, {"items": service.list_access_events(
                actor_id,
                version_id=query.get("version_id", [None])[0],
                audience_id=query.get("audience_id", [None])[0],
                since=query.get("since", [None])[0],
                until=query.get("until", [None])[0])}
        if method == "GET" and parsed.path == "/launch/access-event":
            query = parse_qs(parsed.query)
            access_id = query.get("access_id", [""])[0]
            if not access_id:
                raise ValidationError("access_id 不能为空")
            return 200, service.explain_access(actor_id, access_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: LaunchService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动技术首发与受限披露管理 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技术首发与受限披露管理服务")
    parser.add_argument("--database", default="launch_disclosure.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = LaunchDatabase(args.database)
    Handler.service = LaunchService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
