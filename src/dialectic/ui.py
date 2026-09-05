"""Local UI ingress for the bounded Dialectic workflows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
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

from .app_logging import (
    close_structured_logging,
    configure_structured_logging,
    log_event,
)
from .contracts import ResearchMode, RunMode
from .desktop import load_desktop_responses, load_desktop_web_sources
from .runtime import build_service
from .service import DialecticService
from .ui_config import (
    ModeratorMode,
    RuntimeName,
    UiAgentChoice,
    UiRunConfig,
    build_config_bytes,
)

_MAX_REQUEST_BYTES = 262_144
_MAX_PREVIEW_BYTES = 1_048_576
_MAX_RESPONSE_EXCERPT_CHARS = 280
_MAX_SUMMARY_BRIEF_CHARS = 4_000
_IDLE_SHUTDOWN_SECONDS = 30 * 60
_LOGGER = logging.getLogger("dialectic.ui")

_MODEL_CATALOG: dict[RuntimeName, tuple[tuple[str, str, str], ...]] = {
    "codex": (
        ("gpt-5.6-sol", "GPT-5.6 Sol", "Flagship capability for complex work"),
        ("gpt-6-astra", "GPT-6 Astra", "Most capable model for complex, demanding work"),
        ("gpt-5.6-terra", "GPT-5.6 Terra", "Balanced capability and cost"),
        ("gpt-5.6-luna", "GPT-5.6 Luna", "Fast, economical high-volume work"),
    ),
    "claude-code": (
        ("claude-opus-5", "Claude Opus 5", "Deep reasoning and agentic coding"),
        ("claude-sonnet-5", "Claude Sonnet 5", "Balanced speed and intelligence"),
        ("claude-fable-5", "Claude Fable 5", "Long-running agent work"),
        ("claude-haiku-4-5", "Claude Haiku 4.5", "Fastest Claude option"),
    ),
    "grok-build": (
        ("grok-4.6", "Grok 4.6", "Current Grok Build default"),
        ("grok-build-0.1", "Grok Build 0.1", "Coding-focused model"),
    ),
}

_MODEL_ENVIRONMENT: dict[RuntimeName, tuple[str, ...]] = {
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

_RUNTIME_EXECUTABLES: dict[RuntimeName, str] = {
    "codex": "codex",
    "claude-code": "claude",
    "grok-build": "grok",
}


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    mode: RunMode
    research_mode: ResearchMode
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
    research_mode = _string(payload.get("researchMode", "offline"), "research mode")
    if research_mode not in {"offline", "live-web"}:
        raise ValueError("Research mode must be offline or live-web")

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
    moderator_mode = "fresh"
    if mode == "council":
        moderator_mode = _string(
            payload.get("moderatorMode", "fresh"), "moderator mode"
        )
    config_bytes = build_config_bytes(
        UiRunConfig(
            mode=cast(RunMode, mode),
            main_runtime=cast(RuntimeName, main_runtime),
            main_model=main_model,
            main_effort=main_effort,
            agents=tuple(agents),
            research_mode=cast(ResearchMode, research_mode),
            max_dissenters=max_dissenters,
            moderator_mode=cast(ModeratorMode, moderator_mode),
            native_executables=cast(Any, payload.get("nativeExecutables", {})),
        )
    )

    repository: Path | None = None
    if mode == "code":
        raw_repository = _string(payload.get("repository"), "repository").strip()
        if not raw_repository:
            raise ValueError("Repository is required in Code mode")
        repository = _resolve_repository_path(raw_repository)
        if not repository.is_dir():
            raise ValueError("Repository must be an existing directory")

    return _PreparedRun(
        mode=cast(RunMode, mode),
        research_mode=cast(ResearchMode, research_mode),
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


def _is_wsl() -> bool:
    return os.name != "nt" and bool(os.environ.get("WSL_DISTRO_NAME"))


def _windows_bridge_config() -> tuple[Path, str] | None:
    if not _is_wsl():
        return None
    raw_directory = os.environ.get("DIALECTIC_WINDOWS_BRIDGE_DIR", "")
    token = os.environ.get("DIALECTIC_WINDOWS_BRIDGE_TOKEN", "")
    if not raw_directory or len(token) < 16:
        return None
    try:
        directory = Path(raw_directory).resolve(strict=True)
    except OSError:
        return None
    if not directory.is_dir():
        return None
    return directory, token


def _write_bridge_message(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _request_windows_bridge(action: str) -> dict[str, object]:
    bridge = _windows_bridge_config()
    if bridge is None:
        raise ValueError("The Windows repository picker bridge is unavailable; enter the path directly")
    directory, token = bridge
    request_id = secrets.token_hex(16)
    request = directory / f"request-{request_id}.json"
    response = directory / f"response-{request_id}.json"
    _write_bridge_message(request, {"token": token, "action": action})
    deadline = time.monotonic() + 300
    try:
        while time.monotonic() < deadline:
            if response.is_file():
                try:
                    payload = json.loads(response.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError("The Windows repository picker returned an invalid response") from exc
                if not isinstance(payload, dict):
                    raise ValueError("The Windows repository picker returned an invalid response")
                return payload
            if not (directory / ".ready").is_file():
                raise ValueError(
                    "The Windows repository picker bridge stopped. Exit Dialectic and launch it again."
                )
            time.sleep(0.05)
    finally:
        for path in (request, response):
            try:
                path.unlink()
            except OSError:
                pass
    raise ValueError("The Windows repository picker timed out after five minutes")


def _resolve_repository_path(raw_repository: str) -> Path:
    candidate = raw_repository
    if _is_wsl() and re.match(r"^[A-Za-z]:[\\/]", raw_repository):
        try:
            translated = subprocess.run(
                ["wslpath", "-a", "-u", raw_repository],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Could not translate the selected Windows repository path") from exc
        if translated.returncode != 0:
            diagnostic = translated.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(
                "Could not translate the selected Windows repository path"
                + (f": {diagnostic}" if diagnostic else "")
            )
        candidate = translated.stdout.decode("utf-8", errors="strict").strip()
        if not candidate:
            raise ValueError("Could not translate the selected Windows repository path")
    return Path(candidate).expanduser().resolve()


class _UiState:
    def __init__(self, log_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._active = False
        self._last_seen = time.monotonic()
        self._service: DialecticService | None = None
        self._snapshot: dict[str, object] = {
            "active": False,
            "runId": "",
            "phase": "",
            "status": "READY",
            "outcome": "",
            "failure": "",
            "artifactDir": "",
            "summaryPath": "",
            "summaryBrief": "",
            "logPath": str(log_path) if log_path is not None else "",
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
            snapshot = dict(self._snapshot)
            service = self._service
        run_id = snapshot.get("runId")
        snapshot["turnTiming"] = (
            service.turn_deadline_snapshot(run_id)
            if service is not None and isinstance(run_id, str) and run_id
            else {
                "active": False,
                "turnCount": 0,
                "remainingSeconds": 0,
                "effectiveRemainingSeconds": 0,
                "canExtend": False,
                "turns": [],
            }
        )
        artifact_dir = snapshot.get("artifactDir")
        snapshot["responses"] = (
            _response_excerpts(Path(artifact_dir))
            if isinstance(artifact_dir, str) and artifact_dir
            else []
        )
        snapshot["sources"] = (
            _source_excerpts(Path(artifact_dir))
            if isinstance(artifact_dir, str) and artifact_dir
            else []
        )
        return snapshot

    def extend_turns(self) -> dict[str, object]:
        with self._lock:
            service = self._service
            run_id = self._snapshot.get("runId")
        if service is None or not isinstance(run_id, str) or not run_id:
            raise ValueError("no active turn is available to extend")
        return service.extend_turn_deadlines(run_id)

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
                    "summaryBrief": "",
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
            "log": "logPath",
            "worktree": "worktree",
        }.get(target)
        if key is None:
            raise ValueError("Unknown result target")
        raw = self.snapshot().get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"No {target} path is available")
        return Path(raw).resolve(strict=True)

    def content_for(self, target: str) -> dict[str, object]:
        if target not in {"log", "summary"}:
            raise ValueError("Only the app log and summary can be previewed")
        path = self.path_for(target)
        content, truncated = _text_preview(path, tail=target == "log")
        return {
            "title": "App log" if target == "log" else "Run summary",
            "path": str(path),
            "content": content,
            "truncated": truncated,
        }

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
        log_event(
            _LOGGER,
            logging.INFO,
            "ui.run_requested",
            mode=prepared.mode,
            research_mode=prepared.research_mode,
        )
        try:
            service = build_service()
            with self._lock:
                self._service = service
            service.set_progress_observer(self._progress)
            handle = service.create_run(prepared.mode)
            with self._lock:
                self._snapshot.update(
                    {
                        "runId": handle.run_id,
                        "artifactDir": str(handle.path),
                    }
                )
            log_event(
                _LOGGER,
                logging.INFO,
                "ui.run_created",
                run_id=handle.run_id,
                mode=prepared.mode,
            )
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
            summary_path = artifact_dir / "summary.md"
            summary_brief = _summary_brief(summary_path) or failure
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
                        "summaryPath": str(summary_path),
                        "summaryBrief": summary_brief,
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
            log_event(
                _LOGGER,
                logging.INFO if record.status == "FINALIZED" else logging.WARNING,
                "ui.run_completed",
                run_id=record.run_id,
                mode=record.mode,
                status=record.status,
                failure_kind=record.failure_kind,
                failure_detail=record.failure_detail,
            )
        except Exception as exc:
            run_id = self.snapshot().get("runId")
            log_event(
                _LOGGER,
                logging.ERROR,
                "ui.run_error",
                run_id=run_id if isinstance(run_id, str) else None,
                exception_type=type(exc).__name__,
            )
            with self._lock:
                failure = f"{type(exc).__name__}: {exc}"
                self._snapshot.update(
                    {
                        "active": False,
                        "phase": "-",
                        "status": "UI_ERROR",
                        "failure": failure,
                        "summaryBrief": failure,
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
            elif parsed.path == "/api/content":
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be an object")
                target = _string(payload.get("target"), "target")
                self._json(HTTPStatus.OK, self.server.ui_state.content_for(target))
            elif parsed.path == "/api/extend":
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be an object")
                self._json(HTTPStatus.OK, self.server.ui_state.extend_turns())
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
    models: dict[RuntimeName, list[dict[str, str]]] = {}
    for runtime, catalog in _MODEL_CATALOG.items():
        catalog_by_id = {
            model_id: {
                "id": model_id,
                "name": name,
                "description": description,
                "source": "catalog",
            }
            for model_id, name, description in catalog
        }
        choices: list[dict[str, str]] = []
        seen: set[str] = set()
        for environment_name in _MODEL_ENVIRONMENT[runtime]:
            selector = os.environ.get(environment_name, "").strip()
            if not selector or selector in seen:
                continue
            configured = dict(
                catalog_by_id.get(
                    selector,
                    {
                        "id": selector,
                        "name": f"Configured model ({selector})",
                        "description": "Provided by the local environment",
                        "source": "environment",
                    },
                )
            )
            configured["source"] = "environment"
            choices.append(configured)
            seen.add(selector)
        for model_id, choice in catalog_by_id.items():
            if model_id not in seen:
                choices.append(choice)
        models[runtime] = choices

    runtimes = {
        runtime: {
            "installed": shutil.which(executable) is not None,
            "executable": executable,
        }
        for runtime, executable in _RUNTIME_EXECUTABLES.items()
    }
    return {
        "models": models,
        "runtimes": runtimes,
        "browseSupported": os.name == "nt" or _windows_bridge_config() is not None,
    }


def _choose_repository() -> str:
    if _is_wsl():
        payload = _request_windows_bridge("browse")
        if isinstance(payload.get("error"), str):
            raise ValueError(f"Could not open the Windows repository picker: {payload['error']}")
        if not isinstance(payload.get("path"), str):
            raise ValueError("The Windows repository picker returned an invalid response")
        return payload["path"]
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


def _text_preview(path: Path, *, tail: bool) -> tuple[str, bool]:
    if not path.is_file():
        raise ValueError("Preview target is not a file")
    size = path.stat().st_size
    truncated = size > _MAX_PREVIEW_BYTES
    with path.open("rb") as stream:
        if truncated and tail:
            stream.seek(size - _MAX_PREVIEW_BYTES)
        data = stream.read(_MAX_PREVIEW_BYTES)
    text = data.decode("utf-8", errors="replace")
    if truncated:
        marker = "… earlier content omitted …\n" if tail else "\n… later content omitted …"
        text = marker + text if tail else text + marker
    return text, truncated


def _summary_brief(path: Path) -> str:
    """Return the final answer or concise terminal notes from a persisted summary."""
    try:
        text, _truncated = _text_preview(path, tail=False)
    except (OSError, ValueError):
        return ""
    lines = text.splitlines()
    try:
        answer_start = next(
            index + 1
            for index, line in enumerate(lines)
            if line.strip() == "## Council answer"
        )
    except StopIteration:
        answer_start = -1
    if answer_start >= 0:
        answer_lines: list[str] = []
        for line in lines[answer_start:]:
            if line.startswith("## "):
                break
            answer_lines.append(line)
        brief = "\n".join(answer_lines).strip()
    else:
        notes = [
            line
            for line in lines
            if not line.startswith("# ")
            and not line.startswith("Status: ")
            and not line.startswith("Outcome: ")
            and not line.startswith("Failure: ")
        ]
        brief = "\n".join(notes).strip()
    if len(brief) > _MAX_SUMMARY_BRIEF_CHARS:
        return brief[: _MAX_SUMMARY_BRIEF_CHARS - 1].rstrip() + "…"
    return brief


def _response_excerpts(artifact_dir: Path) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = []
    for response in load_desktop_responses(artifact_dir):
        compact = " ".join(response.text.split())
        if len(compact) > _MAX_RESPONSE_EXCERPT_CHARS:
            compact = compact[: _MAX_RESPONSE_EXCERPT_CHARS - 1] + "…"
        responses.append(
            {
                "role": response.role,
                "targetId": response.target_id,
                "phase": response.phase,
                "runtime": response.runtime,
                "model": response.model,
                "excerpt": compact,
                "status": response.status,
                "completedAt": response.completed_at,
                "durationSeconds": response.duration_seconds,
            }
        )
    return responses


def _source_excerpts(artifact_dir: Path) -> list[dict[str, str]]:
    return [
        {
            "role": source.role,
            "targetId": source.target_id,
            "phase": source.phase,
            "title": source.title,
            "url": source.url,
            "claimContext": source.claim_context,
            "capturedAt": source.captured_at,
        }
        for source in load_desktop_web_sources(artifact_dir)[:100]
    ]


def _ui_html() -> bytes:
    return files("dialectic").joinpath("ui.html").read_bytes()


def _find_windows_app_browser() -> Path | None:
    for executable_name in ("msedge.exe", "chrome.exe"):
        discovered = shutil.which(executable_name)
        if discovered:
            return Path(discovered)

    if _is_wsl():
        for candidate in (
            Path("/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ):
            if candidate.is_file():
                return candidate
        return None

    locations = (
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    )
    for environment_name, relative_path in locations:
        base = os.environ.get(environment_name)
        if base:
            candidate = Path(base, *relative_path.split("/"))
            if candidate.is_file():
                return candidate
    return None


def _launch_chromium_app(executable: Path, url: str) -> bool:
    try:
        subprocess.Popen(
            [
                os.fspath(executable),
                "--new-window",
                f"--app={url}",
                "--no-first-run",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return True


def _open_ui(url: str) -> bool:
    if os.name == "nt" or _is_wsl():
        executable = _find_windows_app_browser()
        if executable is not None and _launch_chromium_app(executable, url):
            return True
    return webbrowser.open(url)


def _idle_monitor(server: _UiServer) -> None:
    while True:
        time.sleep(30)
        snapshot = server.ui_state.snapshot()
        if not snapshot["active"] and server.ui_state.idle_for() >= _IDLE_SHUTDOWN_SECONDS:
            server.shutdown()
            return


def _shutdown_windows_bridge() -> None:
    bridge = _windows_bridge_config()
    if bridge is None:
        return
    directory, token = bridge
    shutdown = directory / f"shutdown-{secrets.token_hex(16)}.json"
    try:
        _write_bridge_message(shutdown, {"token": token})
    except OSError:
        return


def main() -> None:
    try:
        log_path = configure_structured_logging("ui")
    except Exception as exc:
        print(f"Warning: structured application log is unavailable ({type(exc).__name__})")
        log_path = None
    state = _UiState(log_path)
    token = secrets.token_urlsafe(32)
    server = _UiServer(("127.0.0.1", 0), token, state)
    threading.Thread(target=_idle_monitor, args=(server,), daemon=True).start()
    url = f"{server.origin}/?token={token}"
    if log_path is not None:
        log_event(
            _LOGGER,
            logging.INFO,
            "application.started",
            origin=server.origin,
            log_path=str(log_path),
        )
    if not _open_ui(url):
        print(f"Open {url}")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _shutdown_windows_bridge()
        if log_path is not None:
            log_event(_LOGGER, logging.INFO, "application.stopped")
            close_structured_logging()


if __name__ == "__main__":
    main()
