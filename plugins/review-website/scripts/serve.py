#!/usr/bin/env python3
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, quote, urlsplit

from zen_agent.review_feedback import ReviewFeedbackError, ReviewFeedbackStore


def _manifest(site: Path) -> dict:
    value = json.loads((site / "site-manifest.json").read_text(encoding="utf-8"))
    run_id = value.get("site_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("site manifest has no run identity")
    return value


def _approval_allowed(site: Path, item_id: str) -> bool:
    review = json.loads((site / "review.json").read_text(encoding="utf-8"))
    return any(
        row.get("human_review", {}).get("item_id") == item_id
        and row.get("terminal", {}).get("status") == "VERIFIED_CANDIDATE"
        for row in review.get("conversations", [])
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Host a protected Zen review website")
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()
    site = args.site.resolve()
    if not site.is_dir() or not (site / "site-manifest.json").is_file():
        raise ValueError("site directory has no validated site-manifest.json")
    manifest = _manifest(site)
    run_id = manifest["site_run_id"]
    root = site.parents[2]
    review_db = root / ".zen" / "review-feedback.db"
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_network:
        raise PermissionError("non-loopback binding requires --allow-network and human approval")
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    token = args.token or os.environ.get("ZEN_REVIEW_TOKEN") or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ZenReview/0.2"

        def _authenticated(self) -> bool:
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if secrets.compare_digest(supplied, token):
                return True
            cookie = SimpleCookie(self.headers.get("Cookie"))
            value = cookie.get("zen_review_token")
            return bool(value and secrets.compare_digest(value.value, token))

        def _headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def _json(self, status: HTTPStatus, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self) -> bool:
            if self._authenticated():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            return False

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query_token = parse_qs(parsed.query).get("token", [""])[0]
            if query_token and secrets.compare_digest(query_token, token):
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"zen_review_token={token}; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not self._require_auth():
                return
            if parsed.path == "/api/review-state":
                with ReviewFeedbackStore(review_db) as store:
                    items = store.list_items(run_id=run_id, limit=10_000)
                    values = [store.get_item(item["id"]) for item in items]
                self._json(HTTPStatus.OK, {"run_id": run_id, "items": values})
                return
            requested = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            if requested not in {"index.html", "app.js", "styles.css", "review.json"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target = (site / requested).resolve()
            if site not in target.parents or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/json":
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self._headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self._require_auth():
                return
            if urlsplit(self.path).path != "/api/review-decisions":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("X-Zen-Review") != "1":
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing review request guard"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 65_536:
                    raise ValueError("request body must be between 1 and 65536 bytes")
                value = json.loads(self.rfile.read(length))
                allowed = {
                    "item_id", "action", "reviewer_identity", "idempotency_key",
                    "feedback", "assistant_edits",
                }
                if not isinstance(value, dict) or set(value) - allowed:
                    raise ValueError("unsupported request fields")
                required = allowed - {"assistant_edits"}
                if required - set(value):
                    raise ValueError("missing required request fields")
                if value["action"] == "APPROVE" and not _approval_allowed(site, value["item_id"]):
                    raise ReviewFeedbackError("only independently verified candidates can be approved")
                with ReviewFeedbackStore(review_db) as store:
                    item = store.get_item(value["item_id"], include_history=False)
                    if item["run_id"] != run_id:
                        raise ReviewFeedbackError("review item belongs to another run")
                    decision = store.record_decision(
                        value["item_id"], action=value["action"],
                        reviewer_identity=value["reviewer_identity"],
                        idempotency_key=value["idempotency_key"],
                        feedback=value["feedback"],
                        assistant_edits=value.get("assistant_edits"),
                    )
                    updated = store.get_item(value["item_id"])
                self._json(HTTPStatus.CREATED, {"decision": decision, "item": updated})
            except (ValueError, KeyError, ReviewFeedbackError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    host = "127.0.0.1" if args.host == "localhost" else args.host
    print(f"Zen review site: http://{host}:{server.server_port}/?token={quote(token)}", flush=True)
    print("Restricted data: keep this URL private. Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
