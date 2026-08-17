#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen


def _secret(name: str, prompt: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if not sys.stdin.isatty():
        raise RuntimeError(f"{name} is missing; rerun interactively so the harness can request it")
    value = getpass.getpass(prompt).strip()
    if not value:
        raise ValueError(f"{name} cannot be empty")
    return value


def _loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _public_url(api_port: int, process: subprocess.Popen, timeout: float = 20.0) -> str:
    endpoint = f"http://127.0.0.1:{api_port}/api/tunnels"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("ngrok exited before establishing a tunnel")
        try:
            with urlopen(endpoint, timeout=1.0) as response:
                payload = json.load(response)
            urls = [
                item["public_url"]
                for item in payload.get("tunnels", [])
                if item.get("proto") == "https"
            ]
            if urls:
                return urls[0]
        except (OSError, URLError, ValueError, KeyError):
            pass
        time.sleep(0.25)
    raise TimeoutError("ngrok did not expose an HTTPS URL within 20 seconds")


def _tail(path: Path, *secrets_to_remove: str) -> str:
    if not path.is_file():
        return "no diagnostics were emitted"
    value = path.read_text(encoding="utf-8", errors="replace")[-4000:].strip()
    for secret in secrets_to_remove:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value or "no diagnostics were emitted"


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactively publish a protected Zen review site through ngrok"
    )
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--local-port", type=int, default=8765)
    args = parser.parse_args()
    site = args.site.resolve()
    if not site.is_dir() or not (site / "site-manifest.json").is_file():
        raise ValueError("site directory has no validated site-manifest.json")
    if not 1 <= args.local_port <= 65535:
        raise ValueError("local port must be between 1 and 65535")
    ngrok = shutil.which("ngrok")
    if ngrok is None:
        raise RuntimeError("ngrok CLI is not installed")

    print("External publication requested for restricted conversation data.")
    print("The ngrok authtoken is used only in child-process memory and is not persisted.")
    authtoken = _secret("NGROK_AUTHTOKEN", "ngrok authtoken (input hidden): ")
    review_token = os.environ.get("ZEN_REVIEW_TOKEN") or secrets.token_urlsafe(32)
    local_env = dict(os.environ)
    local_env.pop("NGROK_AUTHTOKEN", None)
    local_env["ZEN_REVIEW_TOKEN"] = review_token
    tunnel_env = dict(os.environ)
    tunnel_env.pop("ZEN_REVIEW_TOKEN", None)
    tunnel_env["NGROK_AUTHTOKEN"] = authtoken
    local_script = Path(__file__).with_name("serve.py")
    api_port = _loopback_port()

    local = None
    tunnel = None
    with tempfile.TemporaryDirectory(prefix="zen-ngrok-") as temporary:
        runtime = Path(temporary)
        config = runtime / "ngrok.yml"
        config.write_text(
            "version: 3\n"
            "agent:\n"
            f"  web_addr: 127.0.0.1:{api_port}\n"
            "  console_ui: false\n"
            "  update_check: false\n",
            encoding="utf-8",
        )
        os.chmod(config, 0o600)
        local_error = runtime / "local.stderr.log"
        ngrok_error = runtime / "ngrok.stderr.log"
        with local_error.open("w", encoding="utf-8") as local_stderr, ngrok_error.open(
            "w", encoding="utf-8"
        ) as ngrok_stderr:
            os.chmod(local_error, 0o600)
            os.chmod(ngrok_error, 0o600)
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
                    stderr=local_stderr,
                    text=True,
                )
                time.sleep(0.4)
                if local.poll() is not None:
                    raise RuntimeError(
                        f"local review server failed with code {local.returncode}: "
                        + _tail(local_error, review_token, authtoken)
                    )
                tunnel = subprocess.Popen(
                    [
                        ngrok,
                        "http",
                        f"http://127.0.0.1:{args.local_port}",
                        "--config",
                        str(config),
                        "--log",
                        "stderr",
                        "--log-format",
                        "json",
                    ],
                    env=tunnel_env,
                    stdout=subprocess.DEVNULL,
                    stderr=ngrok_stderr,
                    text=True,
                )
                public = _public_url(api_port, tunnel)
                print(f"Protected Zen review site: {public}/?token={quote(review_token)}", flush=True)
                print("Keep this URL private. It grants access to restricted conversations.", flush=True)
                print("Press Ctrl-C to close both the tunnel and local server.", flush=True)
                while local.poll() is None and tunnel.poll() is None:
                    time.sleep(0.5)
                if local.poll() is not None:
                    raise RuntimeError(
                        f"local review server stopped with code {local.returncode}: "
                        + _tail(local_error, review_token, authtoken)
                    )
                raise RuntimeError(
                    f"ngrok stopped with code {tunnel.returncode}: "
                    + _tail(ngrok_error, review_token, authtoken)
                )
            except KeyboardInterrupt:
                print("Stopping protected review site and ngrok tunnel.", flush=True)
                return 0
            finally:
                _stop(tunnel)
                _stop(local)


if __name__ == "__main__":
    raise SystemExit(main())
