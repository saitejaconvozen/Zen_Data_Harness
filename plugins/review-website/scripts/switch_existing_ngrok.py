#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def _api(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        base.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5.0) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"ngrok Agent API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ngrok Agent API unavailable: {exc.reason}") from exc


def _agent_base(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("agent API must be a loopback HTTP address")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("agent API must not include path, query, or fragment")
    return value.rstrip("/")


def _write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _endpoint(base: str, name: str, addr: str, inspect: bool) -> dict:
    return _api(
        base,
        "POST",
        "/api/tunnels",
        {"name": name, "addr": addr, "proto": "http", "inspect": inspect},
    )


def _delete(base: str, name: str) -> None:
    _api(base, "DELETE", "/api/tunnels/" + quote(name, safe=""))


def _local_server_ready(url: str) -> None:
    try:
        with urlopen(url, timeout=3.0) as response:
            status = response.status
    except HTTPError as exc:
        status = exc.code
    except URLError as exc:
        raise RuntimeError(f"review server is unreachable: {exc.reason}") from exc
    if status not in {200, 401}:
        raise RuntimeError(f"review server returned unexpected HTTP {status}")


def activate(args) -> dict:
    base = _agent_base(args.agent_api)
    state_path = args.state.resolve()
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") == "ACTIVE":
            raise RuntimeError("a review tunnel switch is already active; restore it first")
    _local_server_ready(args.review_upstream)
    inventory = _api(base, "GET", "/api/tunnels").get("tunnels", [])
    original = next((item for item in inventory if item.get("name") == args.original_name), None)
    if original is None:
        raise RuntimeError(f"original ngrok endpoint {args.original_name!r} was not found")
    original_addr = original.get("config", {}).get("addr")
    if original_addr != args.expected_original_upstream:
        raise RuntimeError(
            f"original endpoint upstream changed; expected {args.expected_original_upstream!r}, observed {original_addr!r}"
        )
    state = {
        "schema_version": "zen.ngrok-review-switch/1",
        "status": "PREPARED",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "agent_api": base,
        "original": {
            "name": original["name"],
            "addr": original_addr,
            "inspect": bool(original.get("config", {}).get("inspect", True)),
            "public_url": original.get("public_url"),
        },
        "review": {
            "name": args.review_name,
            "addr": args.review_upstream,
            "inspect": False,
        },
    }
    _write_state(state_path, state)
    _delete(base, original["name"])
    try:
        review = _endpoint(base, args.review_name, args.review_upstream, False)
    except Exception:
        _endpoint(base, original["name"], original_addr, state["original"]["inspect"])
        state["status"] = "ROLLED_BACK"
        state["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(state_path, state)
        raise
    public = review.get("public_url")
    if not isinstance(public, str) or not public.startswith("https://"):
        _delete(base, args.review_name)
        _endpoint(base, original["name"], original_addr, state["original"]["inspect"])
        state["status"] = "ROLLED_BACK"
        state["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(state_path, state)
        raise RuntimeError("review endpoint did not return an HTTPS URL; original endpoint restored")
    state["status"] = "ACTIVE"
    state["activated_at"] = datetime.now(timezone.utc).isoformat()
    state["review"]["public_url"] = public
    state["switch_id"] = secrets.token_hex(16)
    _write_state(state_path, state)
    return {"status": "ACTIVE", "public_url": public, "state": str(state_path)}


def restore(args) -> dict:
    state_path = args.state.resolve()
    if not state_path.is_file():
        raise FileNotFoundError(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "ACTIVE":
        raise RuntimeError(f"switch is not active: {state.get('status')}")
    base = _agent_base(state["agent_api"])
    inventory = _api(base, "GET", "/api/tunnels").get("tunnels", [])
    names = {item.get("name") for item in inventory}
    review_name = state["review"]["name"]
    if review_name in names:
        _delete(base, review_name)
    original = state["original"]
    if original["name"] not in names:
        restored = _endpoint(base, original["name"], original["addr"], original["inspect"])
    else:
        restored = next(item for item in inventory if item.get("name") == original["name"])
    state["status"] = "RESTORED"
    state["restored_at"] = datetime.now(timezone.utc).isoformat()
    state["restored_public_url"] = restored.get("public_url")
    _write_state(state_path, state)
    return {
        "status": "RESTORED",
        "original_upstream": original["addr"],
        "public_url": restored.get("public_url"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reversibly switch an existing ngrok endpoint to the Zen review site")
    parser.add_argument("--state", type=Path, default=Path(".zen/runtime/ngrok-review-switch.json"))
    parser.add_argument("--agent-api", default="http://127.0.0.1:4040")
    sub = parser.add_subparsers(dest="action", required=True)
    activate_parser = sub.add_parser("activate")
    activate_parser.add_argument("--original-name", default="command_line")
    activate_parser.add_argument("--expected-original-upstream", default="http://localhost:8010")
    activate_parser.add_argument("--review-name", default="zen-review-site")
    activate_parser.add_argument("--review-upstream", default="http://10.120.0.106:8766")
    sub.add_parser("restore")
    args = parser.parse_args()
    result = activate(args) if args.action == "activate" else restore(args)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
