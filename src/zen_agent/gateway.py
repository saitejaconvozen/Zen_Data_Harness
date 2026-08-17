from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from socketserver import BaseServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .coding_state import CodingStateStore


MAX_REQUEST_BYTES = 1_048_576


class GatewayRequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class CodingGatewayServer(ThreadingHTTPServer):
    """Local control-plane HTTP server with an injected durable state store."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        store: CodingStateStore,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        if not _is_loopback(host):
            raise ValueError("coding gateway may bind only to a loopback address")
        self.store = store
        super().__init__((host, port), CodingGatewayHandler)


class CodingGatewayHandler(BaseHTTPRequestHandler):
    server: CodingGatewayServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(
            HTTPStatus.NO_CONTENT,
            None,
            extra_headers={"Allow": "GET, POST, PATCH, OPTIONS"},
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
            self._send_json(status, payload)
        except GatewayRequestError as exc:
            self._send_error(exc.status, exc.code, exc.message)
        except KeyError as exc:
            message = str(exc.args[0]) if exc.args else "resource not found"
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", message)
        except (ValueError, TypeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "the gateway could not complete the request",
            )

    def _route(self, method: str) -> tuple[int, Any]:
        parsed = urlsplit(self.path)
        segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
        query = parse_qs(parsed.query)

        if method == "GET" and segments == ["health"]:
            return HTTPStatus.OK, {"status": "ok", "service": "zen-coding-gateway"}

        if segments == ["v1", "sessions"]:
            if method == "GET":
                limit = _integer_query(query, "limit", 50)
                status = _single_query(query, "status")
                sessions = self.server.store.list_sessions(limit=limit, status=status)
                return HTTPStatus.OK, {"sessions": sessions, "count": len(sessions)}
            if method == "POST":
                body = self._read_json()
                session_id = self.server.store.create_session(
                    _required_string(body, "objective"),
                    _required_string(body, "workspace"),
                    model=_optional_string(body, "model", "gpt-5.6-sol"),
                    agent_name=_optional_string(body, "agent_name", "coordinator"),
                    parent_session_id=_optional_nullable_string(body, "parent_session_id"),
                    metadata=_optional_object(body, "metadata"),
                    session_id=_optional_nullable_string(body, "id"),
                )
                return HTTPStatus.CREATED, {"session": self.server.store.get_session(session_id)}
            raise self._method_not_allowed("GET, POST")

        if len(segments) >= 3 and segments[:2] == ["v1", "sessions"]:
            session_id = segments[2]
            if not session_id:
                raise GatewayRequestError(HTTPStatus.BAD_REQUEST, "invalid_path", "empty session id")

            if len(segments) == 3:
                if method == "GET":
                    return HTTPStatus.OK, {"session": self.server.store.get_session(session_id)}
                if method == "PATCH":
                    body = self._read_json()
                    session = self.server.store.update_session_status(
                        session_id,
                        _required_string(body, "status"),
                        reason=_optional_nullable_string(body, "reason"),
                    )
                    return HTTPStatus.OK, {"session": session}
                raise self._method_not_allowed("GET, PATCH")

            if len(segments) == 4:
                resource = segments[3]
                if resource == "events" and method == "GET":
                    after = _integer_query(query, "after", 0)
                    limit = _integer_query(query, "limit", 500)
                    events = self.server.store.list_events(session_id, after=after, limit=limit)
                    next_after = events[-1]["id"] if events else after
                    return HTTPStatus.OK, {
                        "events": events,
                        "count": len(events),
                        "next_after": next_after,
                    }
                if resource == "turns" and method == "GET":
                    turns = self.server.store.list_turns(session_id)
                    return HTTPStatus.OK, {"turns": turns, "count": len(turns)}
                if resource == "tool-calls" and method == "GET":
                    calls = self.server.store.list_tool_calls(session_id)
                    return HTTPStatus.OK, {"tool_calls": calls, "count": len(calls)}
                if resource == "feedback" and method == "GET":
                    pending = _boolean_query(query, "pending", False)
                    feedback = self.server.store.list_feedback(session_id, pending_only=pending)
                    return HTTPStatus.OK, {"feedback": feedback, "count": len(feedback)}
                if resource in {"feedback", "steering"} and method == "POST":
                    body = self._read_json()
                    feedback = self.server.store.add_feedback(
                        session_id,
                        _required_string(body, "message"),
                        author=_optional_string(body, "author", "human"),
                        kind=resource,
                    )
                    return HTTPStatus.ACCEPTED, {"feedback": feedback}
                if resource == "cancel" and method == "POST":
                    body = self._read_json(allow_empty=True)
                    session = self.server.store.request_cancel(
                        session_id, reason=_optional_nullable_string(body, "reason")
                    )
                    return HTTPStatus.ACCEPTED, {"session": session}
                if resource in {"events", "turns", "tool-calls", "feedback", "steering", "cancel"}:
                    raise self._method_not_allowed("GET or POST, depending on resource")

        raise GatewayRequestError(HTTPStatus.NOT_FOUND, "not_found", "route not found")

    def _read_json(self, *, allow_empty: bool = False) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise GatewayRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise GatewayRequestError(
                HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required"
            )
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise GatewayRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_content_length", "invalid Content-Length"
            ) from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise GatewayRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                f"JSON body exceeds {MAX_REQUEST_BYTES} bytes",
            )
        raw = self.rfile.read(length)
        if not raw and allow_empty:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "request body is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise GatewayRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be a JSON object"
            )
        return value

    def _method_not_allowed(self, allow: str) -> GatewayRequestError:
        return GatewayRequestError(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            f"method not allowed; expected {allow}",
        )

    def _send_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if raw:
            self.wfile.write(raw)


def create_gateway_server(
    store: CodingStateStore, host: str = "127.0.0.1", port: int = 0
) -> CodingGatewayServer:
    return CodingGatewayServer(store, host, port)


def serve_gateway(
    store: CodingStateStore, host: str = "127.0.0.1", port: int = 8766
) -> None:
    server = create_gateway_server(store, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _single_query(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    if len(values) != 1:
        raise ValueError(f"query parameter {name} must occur once")
    return values[0]


def _integer_query(query: dict[str, list[str]], name: str, default: int) -> int:
    value = _single_query(query, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"query parameter {name} must be an integer") from exc


def _boolean_query(query: dict[str, list[str]], name: str, default: bool) -> bool:
    value = _single_query(query, name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"query parameter {name} must be a boolean")


def _required_string(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(body: dict[str, Any], name: str, default: str) -> str:
    if name not in body:
        return default
    return _required_string(body, name)


def _optional_nullable_string(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value.strip() or None


def _optional_object(body: dict[str, Any], name: str) -> dict[str, Any]:
    value = body.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value
