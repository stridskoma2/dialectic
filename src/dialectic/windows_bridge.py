"""Authenticated file bridge for Windows-only desktop operations from WSL."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from .ui import _choose_repository

_IDLE_SECONDS = 1800
_POLL_SECONDS = 0.05
_REQUEST_PATTERN = re.compile(r"request-([0-9a-f]{32})\.json\Z")
_TOKEN_ENVIRONMENT = "DIALECTIC_WINDOWS_BRIDGE_TOKEN"


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _authorized_payload(path: Path, token: str) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    supplied = payload.get("token")
    if not isinstance(supplied, str) or not secrets.compare_digest(supplied, token):
        return None
    return payload


def _cleanup_bridge_directory(directory: Path) -> None:
    for pattern in (".ready", "request-*.json", "response-*.json", "shutdown-*.json", ".*.tmp"):
        for path in directory.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _run_bridge(
    directory: Path,
    token: str,
    picker: Callable[[], str] = _choose_repository,
    *,
    idle_seconds: float = _IDLE_SECONDS,
    poll_seconds: float = _POLL_SECONDS,
) -> None:
    ready = directory / ".ready"
    ready.touch(exist_ok=False)
    last_seen = time.monotonic()
    try:
        while time.monotonic() - last_seen < idle_seconds:
            for shutdown in directory.glob("shutdown-*.json"):
                payload = _authorized_payload(shutdown, token)
                try:
                    shutdown.unlink()
                except OSError:
                    pass
                if payload is not None:
                    return

            for request in directory.glob("request-*.json"):
                match = _REQUEST_PATTERN.fullmatch(request.name)
                payload = _authorized_payload(request, token)
                try:
                    request.unlink()
                except OSError:
                    pass
                if match is None or payload is None:
                    continue
                last_seen = time.monotonic()
                if payload.get("action") != "browse":
                    response: dict[str, object] = {"error": "Unsupported bridge action"}
                else:
                    try:
                        response = {"path": picker()}
                    except (OSError, ValueError) as exc:
                        response = {"error": str(exc)}
                _write_json_atomic(directory / f"response-{match.group(1)}.json", response)

            time.sleep(poll_seconds)
    finally:
        _cleanup_bridge_directory(directory)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    token = os.environ.get(_TOKEN_ENVIRONMENT, "")
    if len(token) < 16:
        parser.error("token is invalid")
    if not arguments.directory.is_dir():
        parser.error("bridge directory does not exist")

    _run_bridge(arguments.directory.resolve(), token)


if __name__ == "__main__":
    main()
