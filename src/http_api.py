from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

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

        @staticmethod
        def _segments(path: str) -> List[str]:
            return [part for part in path.split("/") if part]

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                seg = self._segments(path)
                actor, role = self._identity()
                del actor
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif seg == ["api", "items"]:
                    self._json(200, {"items": service.list_items(role)})
                elif len(seg) == 3 and seg[:2] == ["api", "items"]:
                    self._json(200, service.get_item(int(seg[2]), role))
                elif len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "records":
                    self._json(200, {"records": service.list_records(int(seg[2]), role)})
                elif len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "zones":
                    self._json(200, {"zones": service.list_zones(int(seg[2]), role)})
                elif seg == ["api", "audit"]:
                    self._json(200, {"events": service.audit(role)})
                elif len(seg) == 3 and seg[:2] == ["api", "zones"]:
                    self._json(200, service.get_zone(int(seg[2]), role))
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "dispatches":
                    self._json(200, {"dispatches": service.list_dispatches(int(seg[2]), role)})
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "materials":
                    self._json(200, {"materials": service.list_materials(int(seg[2]), role)})
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "closure_checklist":
                    self._json(200, service.closure_checklist(int(seg[2]), role))
                elif len(seg) == 3 and seg[:2] == ["api", "dispatches"]:
                    self._json(200, service.get_dispatch(int(seg[2]), role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                seg = self._segments(path)
                actor, role = self._identity()
                body = self._body()
                if seg == ["api", "items"]:
                    self._json(201, service.create_item(body, actor, role))
                elif len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "records":
                    self._json(201, service.add_record(int(seg[2]), body, actor, role))
                elif len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "transition":
                    self._json(200, service.transition(
                        int(seg[2]), body.get("target"),
                        body.get("expected_version"), actor, role))
                elif len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "zones":
                    self._json(201, service.create_zone(int(seg[2]), body, actor, role))
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "reports":
                    result = service.submit_report(int(seg[2]), body, actor, role)
                    self._json(200 if result.get("merged") else 201, result)
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "dispatches":
                    result = service.create_dispatch(int(seg[2]), body, actor, role)
                    self._json(200 if result.get("merged") else 201, result)
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "materials":
                    self._json(201, service.upsert_material(int(seg[2]), body, actor, role))
                elif (len(seg) == 6 and seg[:2] == ["api", "zones"]
                      and seg[3] == "materials" and seg[5] == "deliver"):
                    result = service.deliver_material(
                        int(seg[2]), int(seg[4]), body, actor, role)
                    self._json(200 if result.get("merged") else 201, result)
                elif len(seg) == 4 and seg[:2] == ["api", "zones"] and seg[3] == "close":
                    self._json(200, service.close_zone(int(seg[2]), body, actor, role))
                elif len(seg) == 4 and seg[:2] == ["api", "dispatches"] and seg[3] == "transition":
                    self._json(200, service.transition_dispatch(int(seg[2]), body, actor, role))
                elif (len(seg) == 6 and seg[:2] == ["api", "dispatches"]
                      and seg[3] == "members" and seg[5] == "resolve"):
                    self._json(200, service.resolve_member(
                        int(seg[2]), int(seg[4]), body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
