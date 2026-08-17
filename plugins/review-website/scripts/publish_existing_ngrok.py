#!/usr/bin/env python3
from __future__ import annotations

import argparse
from hashlib import sha256
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


def _agent_request(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"ngrok Agent API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"cannot reach ngrok Agent API: {exc.reason}") from exc


def _validate_agent_api(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("agent API must be an HTTP loopback address")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("agent API must not contain a path, query, or fragment")
    return value.rstrip("/")


def _stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _local_ready(port: int, token: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5.0
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    url = f"http://127.0.0.1:{port}/?token={quote(token)}"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"local review server exited with code {process.returncode}")
        try:
            with opener.open(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.1)
    raise TimeoutError("local review server did not become ready within five seconds")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a protected Zen review site through an existing ngrok agent"
    )
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--local-port", type=int, default=8765)
    parser.add_argument("--agent-api", default="http://127.0.0.1:4040")
    args = parser.parse_args()
    site = args.site.resolve()
    if not site.is_dir() or not (site / "site-manifest.json").is_file():
        raise ValueError("site directory has no validated site-manifest.json")
    if not 1 <= args.local_port <= 65535:
        raise ValueError("local port must be between 1 and 65535")
    agent_api = _validate_agent_api(args.agent_api)
    existing = _agent_request(agent_api, "GET", "/api/tunnels")
    existing_names = {item.get("name") for item in existing.get("tunnels", [])}
    digest = sha256(str(site).encode("utf-8")).hexdigest()[:12]
    name = f"zen-review-{digest}"
    if name in existing_names:
        raise RuntimeError(f"ngrok endpoint {name} already exists; stop its previous publisher first")

    review_token = os.environ.get("ZEN_REVIEW_TOKEN") or secrets.token_urlsafe(32)
    local_env = dict(os.environ)
    local_env.pop("NGROK_AUTHTOKEN", None)
    local_env["ZEN_REVIEW_TOKEN"] = review_token
    local_script = Path(__file__).with_name("serve.py")
    local = None
    created = False
    try:
        local = subprocess.Popen(
            [
                sys.executable,
                str(local_script),
                "--site",
                str(site),
                "--port",
                str(args.local_port),
            ],
            env=local_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _local_ready(args.local_port, review_token, local)
        tunnel = _agent_request(
            agent_api,
            "POST",
            "/api/tunnels",
            {
                "name": name,
                "addr": f"http://127.0.0.1:{args.local_port}",
                "proto": "http",
                "inspect": False,
            },
        )
        created = True
        public = tunnel.get("public_url")
        if not isinstance(public, str) or not public.startswith("https://"):
            raise RuntimeError("ngrok Agent API did not return an HTTPS public URL")
        print("Reusing the already-authenticated ngrok agent; its existing endpoints are unchanged.")
        print(f"Protected Zen review site: {public}/?token={quote(review_token)}", flush=True)
        print("Keep this URL private. It grants access to restricted conversations.", flush=True)
        print("Press Ctrl-C to remove only this review endpoint and stop the local server.", flush=True)
        while local.poll() is None:
            time.sleep(0.5)
        detail = (local.stderr.read() if local.stderr else "")[-2000:]
        raise RuntimeError(f"local review server stopped with code {local.returncode}: {detail}")
    except KeyboardInterrupt:
        print("Stopping the protected review endpoint; the pre-existing ngrok endpoint remains active.")
        return 0
    finally:
        if created:
            try:
                _agent_request(agent_api, "DELETE", f"/api/tunnels/{quote(name, safe='')}")
            except Exception as exc:
                print(f"Warning: could not remove review endpoint {name}: {exc}", file=sys.stderr)
        _stop(local)


if __name__ == "__main__":
    raise SystemExit(main())
