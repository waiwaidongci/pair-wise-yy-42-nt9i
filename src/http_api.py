from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        @staticmethod
        def _segments(path: str):
            return [s for s in path.split("/") if s != ""]

        @staticmethod
        def _parse_id(value: str) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                raise NotFoundError("资源不存在")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                actor, role = self._identity()
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/members":
                    status = query.get("status", [None])[0]
                    self._json(200, {"members": service.list_members(role, status)})
                elif path == "/api/task-areas":
                    status = query.get("status", [None])[0]
                    self._json(200, {"task_areas": service.list_task_areas(role, status)})
                elif path.startswith("/api/task-areas/") and path.endswith("/checklist"):
                    area_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.task_area_checklist(area_id, role))
                elif path.startswith("/api/task-areas/"):
                    area_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.get_task_area(area_id, role))
                elif path == "/api/dispatches":
                    status = query.get("status", [None])[0]
                    area_raw = query.get("task_area_id", [None])[0]
                    area_id = int(area_raw) if area_raw else None
                    self._json(200, {"dispatches": service.list_dispatches(role, status, area_id)})
                elif path.startswith("/api/dispatches/"):
                    dispatch_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.get_dispatch(dispatch_id, role))
                elif path == "/api/audit":
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path == "/api/members":
                    self._json(201, service.create_member(body, actor, role))
                elif path.startswith("/api/members/") and path.endswith("/return"):
                    member_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.return_member(member_id, actor, role))
                elif path == "/api/task-areas":
                    self._json(201, service.create_task_area(body, actor, role))
                elif path.startswith("/api/task-areas/") and path.endswith("/supplies"):
                    area_id = self._parse_id(path.split("/")[3])
                    self._json(201, service.add_supply(area_id, body, actor, role))
                elif path.startswith("/api/task-areas/") and path.endswith("/sign-off"):
                    area_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.sign_off_task_area(area_id, actor, role))
                elif path.startswith("/api/task-areas/") and path.endswith("/confirm"):
                    area_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.confirm_close_task_area(area_id, actor, role))
                elif path.startswith("/api/supplies/") and path.endswith("/fulfill"):
                    supply_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.fulfill_supply(supply_id, body, actor, role))
                elif path == "/api/wind":
                    self._json(200, service.report_wind(body, actor, role))
                elif path == "/api/dispatches":
                    self._json(201, service.create_dispatch(body, actor, role))
                elif path.startswith("/api/dispatches/") and path.endswith("/start"):
                    dispatch_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.start_dispatch(dispatch_id, actor, role))
                elif path.startswith("/api/dispatches/") and path.endswith("/complete"):
                    dispatch_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.complete_dispatch(dispatch_id, actor, role))
                elif path.startswith("/api/dispatches/") and path.endswith("/resolve"):
                    dispatch_id = self._parse_id(path.split("/")[3])
                    self._json(200, service.resolve_dispatch(dispatch_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
