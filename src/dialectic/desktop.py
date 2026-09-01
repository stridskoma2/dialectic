"""Native desktop entry point and read-only presentation helpers."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

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
