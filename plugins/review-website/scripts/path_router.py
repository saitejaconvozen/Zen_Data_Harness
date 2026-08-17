"""Serve several local sites under one public URL by path prefix.

An ngrok free plan gives a single domain. Two tunnels can be registered against
it, but they both claim the same public URL and routing between them is
undefined — one silently wins. Putting a router in front is the honest fix: one
tunnel, one upstream, deterministic dispatch on the path.

    path-router --route /status=http://127.0.0.1:8899 \
                --default http://localhost:8765

`/status` redirects to `/status/` so the browser treats it as a directory and
relative links inside the app resolve under the prefix.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
})
MAX_BODY = 64 * 1024 * 1024


def _parse_route(value: str) -> tuple[str, str]:
    prefix, _, upstream = value.partition("=")
    if not prefix.startswith("/") or not upstream:
        raise argparse.ArgumentTypeError(f"route must be /prefix=http://host:port, got {value!r}")
    return prefix.rstrip("/"), upstream.rstrip("/")


def build_handler(routes: list[tuple[str, str]], default: str):
    # Longest prefix first so /status/api never matches a shorter /s route.
    ordered = sorted(routes, key=lambda item: len(item[0]), reverse=True)

    class Router(BaseHTTPRequestHandler):
        server_version = "ZenPathRouter/1.0"
        protocol_version = "HTTP/1.1"

        def _resolve(self) -> tuple[str, str] | None:
            path = urlsplit(self.path).path
            for prefix, upstream in ordered:
                if path == prefix:
                    return "REDIRECT", prefix + "/"
                if path.startswith(prefix + "/"):
                    return upstream, self.path[len(prefix):]
            return default, self.path

        def _proxy(self, method: str) -> None:
            resolved = self._resolve()
            if resolved is None:
                self.send_error(404)
                return
            upstream, rest = resolved
            if upstream == "REDIRECT":
                self.send_response(301)
                self.send_header("Location", rest)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= MAX_BODY else None
            headers = {
                key: value for key, value in self.headers.items()
                if key.lower() not in HOP_BY_HOP
            }
            request = Request(upstream + rest, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=180) as response:
                    payload = response.read()
                    status, out_headers = response.status, response.headers
            except HTTPError as exc:
                payload = exc.read()
                status, out_headers = exc.code, exc.headers
            except URLError as exc:
                message = f"upstream unavailable: {exc.reason}".encode()
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(message)))
                self.end_headers()
                self.wfile.write(message)
                return
            self.send_response(status)
            for key, value in out_headers.items():
                if key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._proxy("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._proxy("POST")

        def do_HEAD(self) -> None:  # noqa: N802
            self._proxy("HEAD")

        def log_message(self, *_args) -> None:
            return

    return Router


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Path-prefix router for one public URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8900)
    parser.add_argument(
        "--route", action="append", type=_parse_route, default=[],
        help="/prefix=http://upstream, repeatable",
    )
    parser.add_argument("--default", required=True, help="upstream for everything else")
    args = parser.parse_args(argv)
    handler = build_handler(args.route, args.default.rstrip("/"))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"router on http://{args.host}:{args.port}", flush=True)
    for prefix, upstream in args.route:
        print(f"  {prefix}/  -> {upstream}", flush=True)
    print(f"  /         -> {args.default}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
