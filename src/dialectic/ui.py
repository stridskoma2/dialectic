"""Local browser UI ingress for the bounded Dialectic workflows."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

from .contracts import RunMode
from .runtime import build_service
from .ui_config import RuntimeName, UiAgentChoice, UiRunConfig, build_config_bytes

_MAX_REQUEST_BYTES = 262_144
_IDLE_SHUTDOWN_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    mode: RunMode
    config_bytes: bytes
    prompt_bytes: bytes
    repository: Path | None


def _prepare_run(payload: object) -> _PreparedRun:
    if not isinstance(payload, dict):
        raise ValueError("Request body must be an object")
    mode = _string(payload.get("mode"), "mode")
    if mode not in {"code", "council"}:
        raise ValueError("Mode must be code or council")
    prompt = _string(payload.get("prompt"), "prompt").strip()
    if not prompt:
        raise ValueError("Prompt is required")

    raw_main = payload.get("main")
    if not isinstance(raw_main, dict):
        raise ValueError("Main model selection is required")
    main_runtime = _string(raw_main.get("runtime"), "main runtime")
    main_model = _string(raw_main.get("model"), "main model")
    main_effort = _optional_string(raw_main.get("effort"), "main effort")

    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list):
        raise ValueError("Agent selections must be a list")
    agents: list[UiAgentChoice] = []
    for index, raw_agent in enumerate(raw_agents):
        if not isinstance(raw_agent, dict):
            raise ValueError(f"Agent {index + 1} must be an object")
        agents.append(
            UiAgentChoice(
                runtime=cast(Any, _string(raw_agent.get("runtime"), f"agent {index + 1} runtime")),
                model=_optional_string(raw_agent.get("model"), f"agent {index + 1} model"),
                effort=_optional_string(raw_agent.get("effort"), f"agent {index + 1} effort"),
                lens=_optional_string(raw_agent.get("lens"), f"agent {index + 1} focus")
                or "general-correctness",
            )
        )

    max_dissenters = payload.get("maxDissenters", 0)
    if type(max_dissenters) is not int:
        raise ValueError("Allowed dissenters must be an integer")
    config_bytes = build_config_bytes(
        UiRunConfig(
            mode=cast(RunMode, mode),
            main_runtime=cast(RuntimeName, main_runtime),
            main_model=main_model,
            main_effort=main_effort,
            agents=tuple(agents),
            max_dissenters=max_dissenters,
        )
    )

    repository: Path | None = None
    if mode == "code":
        raw_repository = _string(payload.get("repository"), "repository").strip()
        if not raw_repository:
            raise ValueError("Repository is required in Code mode")
        repository = Path(raw_repository).expanduser().resolve()
        if not repository.is_dir():
            raise ValueError("Repository must be an existing directory")

    return _PreparedRun(
        mode=cast(RunMode, mode),
        config_bytes=config_bytes,
        prompt_bytes=prompt.encode("utf-8"),
        repository=repository,
    )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label.capitalize()} must be text")
    return value


def _optional_string(value: object, label: str) -> str:
    if value is None:
        return ""
    return _string(value, label)


class _UiState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = False
        self._last_seen = time.monotonic()
        self._snapshot: dict[str, object] = {
            "active": False,
            "runId": "",
            "phase": "",
            "status": "READY",
            "outcome": "",
            "failure": "",
            "artifactDir": "",
            "summaryPath": "",
            "worktree": "",
            "branch": "",
            "unresolvedCount": 0,
        }

    def touch(self) -> None:
        with self._lock:
            self._last_seen = time.monotonic()

    def idle_for(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_seen

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._snapshot)

    def start(self, prepared: _PreparedRun) -> None:
        with self._lock:
            if self._active:
                raise ValueError("A run is already in progress")
            self._active = True
            self._snapshot.update(
                {
                    "active": True,
                    "runId": "",
                    "phase": "STARTING",
                    "status": "CREATING",
                    "outcome": "",
                    "failure": "",
                    "artifactDir": "",
                    "summaryPath": "",
                    "worktree": "",
                    "branch": "",
                    "unresolvedCount": 0,
                }
            )
        threading.Thread(
            target=self._run,
            args=(prepared,),
            name="dialectic-ui-run",
            daemon=True,
        ).start()

    def path_for(self, target: str) -> Path:
        key = {
            "artifacts": "artifactDir",
            "summary": "summaryPath",
            "worktree": "worktree",
        }.get(target)
        if key is None:
            raise ValueError("Unknown result target")
        raw = self.snapshot().get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"No {target} path is available")
        return Path(raw).resolve(strict=True)

    def _progress(self, record: object) -> None:
        with self._lock:
            self._snapshot.update(
                {
                    "runId": getattr(record, "run_id"),
                    "phase": getattr(record, "phase") or "-",
                    "status": getattr(record, "status"),
                }
            )

    def _run(self, prepared: _PreparedRun) -> None:
        try:
            service = build_service()
            service.set_progress_observer(self._progress)
            handle = service.create_run(prepared.mode)
            with self._lock:
                self._snapshot["runId"] = handle.run_id
            if prepared.mode == "code":
                assert prepared.repository is not None
                record = asyncio.run(
                    service.execute_code_once(
                        handle,
                        config_bytes=prepared.config_bytes,
                        task_bytes=prepared.prompt_bytes,
                        repository_path=prepared.repository,
                    )
                )
            else:
                record = asyncio.run(
                    service.execute_council_once(
                        handle,
                        config_bytes=prepared.config_bytes,
                        prompt_bytes=prepared.prompt_bytes,
                    )
                )
            summary = service.get_result(record.run_id)
            artifact_dir = service.run_artifact_directory(record.run_id)
            workspace = service.get_workspace(record.run_id)
            failure = ""
            if record.failure_kind:
                failure = f"{record.failure_kind}: {record.failure_detail or ''}".strip()
            with self._lock:
                self._snapshot.update(
                    {
                        "active": False,
                        "runId": record.run_id,
                        "phase": record.phase or "-",
                        "status": record.status,
                        "outcome": record.code_outcome or record.consensus_outcome or "",
                        "failure": failure,
                        "artifactDir": str(artifact_dir),
                        "summaryPath": str(artifact_dir / "summary.md"),
                        "worktree": (
                            workspace.dialectic_worktree
                            if workspace is not None and workspace.dialectic_worktree is not None
                            else ""
                        ),
                        "branch": (
                            workspace.dialectic_branch
                            if workspace is not None and workspace.dialectic_branch is not None
                            else ""
                        ),
                        "unresolvedCount": len(summary.unresolved_items),
                    }
                )
        except Exception as exc:
            with self._lock:
                self._snapshot.update(
                    {
                        "active": False,
                        "phase": "-",
                        "status": "UI_ERROR",
                        "failure": f"{type(exc).__name__}: {exc}",
                    }
                )
        finally:
            with self._lock:
                self._active = False


class _UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, state: _UiState) -> None:
        self.token = token
        self.ui_state = state
        self.origin = ""
        super().__init__(address, _RequestHandler)
        host, port = self.server_address
        self.origin = f"http://{host}:{port}"


class _RequestHandler(BaseHTTPRequestHandler):
    server: _UiServer

    def do_GET(self) -> None:
        self.server.ui_state.touch()
        parsed = urlsplit(self.path)
        if parsed.path == "/" and self._query_token(parsed.query):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"dialectic_token={self.server.token}; HttpOnly; SameSite=Strict; Path=/",
            )
            self.end_headers()
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "UI session is not authorized"})
            return
        if parsed.path == "/":
            self._bytes(HTTPStatus.OK, _ui_html(), "text/html; charset=utf-8")
        elif parsed.path == "/api/options":
            self._json(HTTPStatus.OK, _model_options())
        elif parsed.path == "/api/status":
            self._json(HTTPStatus.OK, self.server.ui_state.snapshot())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        self.server.ui_state.touch()
        if not self._authorized() or not self._same_origin():
            self._discard_body()
            self._json(HTTPStatus.FORBIDDEN, {"error": "UI session is not authorized"})
            return
        parsed = urlsplit(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/run":
                self.server.ui_state.start(_prepare_run(payload))
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
            elif parsed.path == "/api/browse":
                self._json(HTTPStatus.OK, {"path": _choose_repository()})
            elif parsed.path == "/api/open":
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be an object")
                target = _string(payload.get("target"), "target")
                _open_path(self.server.ui_state.path_for(target))
                self._json(HTTPStatus.OK, {"ok": True})
            elif parsed.path == "/api/shutdown":
                if self.server.ui_state.snapshot()["active"]:
                    raise ValueError("Wait for the active run to finish before exiting")
                self._json(HTTPStatus.OK, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except (OSError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin requests are not supported"})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _query_token(self, query: str) -> bool:
        supplied = parse_qs(query).get("token", [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, self.server.token)

    def _authorized(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        item = cookie.get("dialectic_token")
        return item is not None and secrets.compare_digest(item.value, self.server.token)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or secrets.compare_digest(origin, self.server.origin)

    def _read_json(self) -> object:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if not 0 <= length <= _MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be UTF-8 JSON") from exc

    def _discard_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if 0 <= length <= _MAX_REQUEST_BYTES:
            self.rfile.read(length)
        else:
            self.close_connection = True

    def _json(self, status: HTTPStatus, payload: object) -> None:
        self._bytes(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _bytes(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(payload)


def _model_options() -> dict[str, object]:
    names = {
        "codex": (
            "CODEX_DRIVER_MODEL",
            "CODEX_REVIEW_MODEL",
            "CODEX_COUNCIL_MODEL",
            "DIALECTIC_CODEX_MODEL",
        ),
        "claude-code": (
            "CLAUDE_REVIEW_MODEL",
            "CLAUDE_COUNCIL_MODEL",
            "DIALECTIC_CLAUDE_MODEL",
        ),
        "grok-build": (
            "GROK_REVIEW_MODEL",
            "GROK_COUNCIL_MODEL",
            "DIALECTIC_GROK_MODEL",
        ),
    }
    models = {
        runtime: list(dict.fromkeys(os.environ[name] for name in variables if os.environ.get(name)))
        for runtime, variables in names.items()
    }
    return {"models": models, "browseSupported": os.name == "nt"}


def _choose_repository() -> str:
    if os.name != "nt":
        raise ValueError("Native folder browsing is available on Windows; enter the path directly")
    import pythoncom
    from win32com.shell import shell, shellcon

    pythoncom.CoInitialize()
    try:
        try:
            result = shell.SHBrowseForFolder(
                0,
                None,
                "Select a Git repository",
                shellcon.BIF_RETURNONLYFSDIRS
                | getattr(shellcon, "BIF_NEWDIALOGSTYLE", 0x0040)
                | shellcon.BIF_EDITBOX,
                None,
                None,
            )
        except Exception as exc:
            raise ValueError(f"Could not open the repository picker: {exc}") from exc
        if result[0] is None:
            return ""
        selected = shell.SHGetPathFromIDList(result[0])
        return selected.decode("mbcs") if isinstance(selected, bytes) else str(selected)
    finally:
        pythoncom.CoUninitialize()


def _open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


def _ui_html() -> bytes:
    return files("dialectic").joinpath("ui.html").read_bytes()


def _idle_monitor(server: _UiServer) -> None:
    while True:
        time.sleep(30)
        snapshot = server.ui_state.snapshot()
        if not snapshot["active"] and server.ui_state.idle_for() >= _IDLE_SHUTDOWN_SECONDS:
            server.shutdown()
            return


def main() -> None:
    state = _UiState()
    token = secrets.token_urlsafe(32)
    server = _UiServer(("127.0.0.1", 0), token, state)
    threading.Thread(target=_idle_monitor, args=(server,), daemon=True).start()
    url = f"{server.origin}/?token={token}"
    if not webbrowser.open(url):
        print(f"Open {url}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
