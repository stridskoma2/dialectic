"""Native desktop entry point and read-only presentation helpers."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .app_logging import (
    close_structured_logging,
    configure_structured_logging,
    log_event,
)

_MAX_ATTEMPT_BYTES = 1_048_576
_LOGGER = logging.getLogger("dialectic.desktop")


@dataclass(frozen=True, slots=True)
class DesktopResponse:
    """One normalized model response projected from a persisted turn attempt."""

    path: Path
    role: str
    target_id: str
    phase: str
    runtime: str
    model: str
    text: str
    status: str
    completed_at: str

    @property
    def identity(self) -> str:
        return str(self.path)


@dataclass(frozen=True, slots=True)
class DesktopWebSource:
    """One bounded citation projected from a controller-authored source index."""

    path: Path
    role: str
    target_id: str
    phase: str
    title: str
    url: str
    claim_context: str
    captured_at: str

    @property
    def identity(self) -> str:
        return f"{self.path}:{self.url}"


def load_desktop_responses(artifact_dir: Path) -> tuple[DesktopResponse, ...]:
    """Load bounded completed responses without treating them as workflow authority."""

    turns = artifact_dir / "turns"
    if not turns.is_dir():
        return ()
    responses: list[DesktopResponse] = []
    for path in turns.glob("*/*/*.attempt.json"):
        try:
            if path.stat().st_size > _MAX_ATTEMPT_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_response = payload.get("response")
        response = raw_response if isinstance(raw_response, dict) else {}
        text = response.get("text")
        status = "response"
        if not isinstance(text, str) or not text.strip():
            text = payload.get("bounded_diagnostic")
            status = "failed"
        if not isinstance(text, str) or not text.strip():
            continue
        responses.append(
            DesktopResponse(
                path=path.resolve(),
                role=str(payload.get("role", "agent")),
                target_id=str(payload.get("target_id", "agent")),
                phase=str(payload.get("turn_phase", "turn")),
                runtime=str(response.get("runtime", "")),
                model=str(response.get("requested_model", "")),
                text=text,
                status=status,
                completed_at=str(
                    payload.get("response_completed_at")
                    or payload.get("capture_completed_at")
                    or ""
                ),
            )
        )
    responses.sort(
        key=lambda item: (
            item.completed_at,
            item.role,
            item.target_id,
            item.phase,
        )
    )
    return tuple(responses)


def load_desktop_web_sources(artifact_dir: Path) -> tuple[DesktopWebSource, ...]:
    """Load bounded HTTPS citation projections without treating them as proof."""

    source_root = artifact_dir / "research" / "sources"
    if not source_root.is_dir():
        return ()
    sources: list[DesktopWebSource] = []
    for path in source_root.glob("*/*/*.json"):
        try:
            if path.stat().st_size > _MAX_ATTEMPT_BYTES:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        role = str(payload.get("role", "agent"))
        target_id = str(payload.get("target_id", "agent"))
        phase = str(payload.get("turn_phase", "turn"))
        captured_at = str(payload.get("captured_at", ""))
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            continue
        for raw_source in raw_sources[:100]:
            if not isinstance(raw_source, dict):
                continue
            url = raw_source.get("url")
            title = raw_source.get("title")
            context = raw_source.get("claim_context")
            if not isinstance(url, str):
                continue
            try:
                parsed_url = urlsplit(url)
            except ValueError:
                continue
            if (
                parsed_url.scheme != "https"
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
            ):
                continue
            if (
                len(url) > 2_048
                or not isinstance(title, str)
                or len(title) > 512
                or not isinstance(context, str)
                or len(context) > 2_048
            ):
                continue
            sources.append(
                DesktopWebSource(
                    path=path.resolve(),
                    role=role,
                    target_id=target_id,
                    phase=phase,
                    title=title,
                    url=url,
                    claim_context=context,
                    captured_at=captured_at,
                )
            )
    sources.sort(
        key=lambda item: (
            item.captured_at,
            item.role,
            item.target_id,
            item.phase,
            item.url,
        )
    )
    return tuple(sources)


def main() -> None:
    try:
        from .desktop_qt import run_desktop
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name or "").startswith("shiboken6"):
            raise SystemExit(
                'The native desktop UI requires PySide6. Install it with '
                '`python -m pip install -e ".[desktop]"`, then run '
                '`dialectic-desktop` again. The `dialectic-ui` web fallback '
                "remains available."
            ) from exc
        raise

    if "--check" in sys.argv[1:]:
        return

    try:
        log_path = configure_structured_logging("ui")
    except Exception as exc:
        print(f"Warning: structured application log is unavailable ({type(exc).__name__})")
        log_path = None
    if log_path is not None:
        log_event(
            _LOGGER,
            logging.INFO,
            "application.started",
            frontend="desktop",
            log_path=str(log_path),
        )
    try:
        run_desktop(log_path)
    finally:
        if log_path is not None:
            log_event(
                _LOGGER,
                logging.INFO,
                "application.stopped",
                frontend="desktop",
            )
            close_structured_logging()


if __name__ == "__main__":
    main()
