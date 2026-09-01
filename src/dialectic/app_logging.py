"""Private, bounded structured application logging for local frontends."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from .store import (
    apply_private_file_security,
    default_state_root,
    ensure_private_directory,
)

ApplicationComponent = Literal["cli", "ui"]

_LOGGER_NAME = "dialectic"
_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_MAX_FIELD_BYTES = 4096
_EVENT_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HANDLER_LOCK = threading.RLock()
_ACTIVE_HANDLER: _PrivateRotatingFileHandler | None = None
_ACTIVE_PATH: Path | None = None


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler whose files inherit and reassert private permissions."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            apply_private_file_security(Path(self.baseFilename))
        except Exception:
            stream.close()
            raise
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        base = Path(self.baseFilename)
        for candidate in base.parent.glob(f"{base.name}*"):
            if candidate.is_file():
                apply_private_file_security(candidate)


class _JsonLineFormatter(logging.Formatter):
    def __init__(self, component: ApplicationComponent) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "dialectic_event", "log.message")
        payload: dict[str, object] = {
            "log_schema_version": 1,
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "application": self.component,
            "logger": record.name,
            "event": event,
            "pid": record.process,
            "thread": record.threadName,
        }
        fields = getattr(record, "dialectic_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if event == "log.message":
            payload["message"] = _bounded_text(record.getMessage())
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def configure_structured_logging(
    component: ApplicationComponent,
    *,
    state_root: Path | str | None = None,
) -> Path:
    """Configure one private JSONL log for the current frontend process."""

    root = Path(state_root) if state_root is not None else default_state_root()
    logs_root = root / "logs"
    ensure_private_directory(root)
    ensure_private_directory(logs_root)
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = logs_root / f"dialectic-{component}-{started}-{os.getpid()}.jsonl"
    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(_JsonLineFormatter(component))
    handler.setLevel(logging.INFO)

    global _ACTIVE_HANDLER, _ACTIVE_PATH
    with _HANDLER_LOCK:
        close_structured_logging()
        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        _ACTIVE_HANDLER = handler
        _ACTIVE_PATH = path
    return path


def close_structured_logging() -> None:
    """Flush and detach the handler owned by this module."""

    global _ACTIVE_HANDLER, _ACTIVE_PATH
    with _HANDLER_LOCK:
        handler = _ACTIVE_HANDLER
        if handler is not None:
            logging.getLogger(_LOGGER_NAME).removeHandler(handler)
            handler.close()
        _ACTIVE_HANDLER = None
        _ACTIVE_PATH = None


def current_structured_log_path() -> Path | None:
    with _HANDLER_LOCK:
        return _ACTIVE_PATH


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: str | int | float | bool | None,
) -> None:
    """Emit one allowlisted-scalar JSON event without arbitrary object serialization."""

    if _EVENT_RE.fullmatch(event) is None:
        raise ValueError("structured log event name is invalid")
    safe_fields: dict[str, object] = {}
    for key, value in fields.items():
        if _FIELD_RE.fullmatch(key) is None:
            raise ValueError("structured log field name is invalid")
        safe_fields[key] = _bounded_text(value) if isinstance(value, str) else value
    logger.log(
        level,
        event,
        extra={"dialectic_event": event, "dialectic_fields": safe_fields},
    )


def _bounded_text(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_FIELD_BYTES:
        return encoded.decode("utf-8")
    return encoded[:_MAX_FIELD_BYTES].decode("utf-8", errors="ignore")
