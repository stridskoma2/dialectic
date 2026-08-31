from __future__ import annotations

import json
import logging
from pathlib import Path

from dialectic.app_logging import (
    close_structured_logging,
    configure_structured_logging,
    current_structured_log_path,
    log_event,
)
from dialectic.service import DialecticService
from dialectic.store import RunStore


def test_application_log_records_shared_run_lifecycle_as_jsonl(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    log_path = configure_structured_logging("ui", state_root=state_root)
    try:
        log_event(logging.getLogger("dialectic.ui"), logging.INFO, "application.started")
        service = DialecticService(
            RunStore(
                state_root,
                run_id_factory=lambda: "20260831T050000Z-aaaaaaaaaa",
            )
        )
        handle = service.create_run("council")
        service.start_run(handle, phase="PREFLIGHT")
        service.fail_run(
            handle,
            "PREFLIGHT_FAILED",
            "Codex CLI 0.152.0 is installed but is not qualified",
        )
        assert current_structured_log_path() == log_path
    finally:
        close_structured_logging()

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == [
        "application.started",
        "run.created",
        "phase.started",
        "run.failed",
    ]
    failure = rows[-1]
    assert failure == {
        "application": "ui",
        "event": "run.failed",
        "failure_detail": "Codex CLI 0.152.0 is installed but is not qualified",
        "failure_kind": "PREFLIGHT_FAILED",
        "level": "WARNING",
        "log_schema_version": 1,
        "logger": "dialectic.service",
        "mode": "council",
        "phase": "PREFLIGHT",
        "pid": failure["pid"],
        "run_id": "20260831T050000Z-aaaaaaaaaa",
        "status": "FAILED",
        "thread": failure["thread"],
        "timestamp": failure["timestamp"],
    }
    assert current_structured_log_path() is None


def test_application_log_bounds_string_fields_without_recording_arbitrary_objects(
    tmp_path: Path,
) -> None:
    log_path = configure_structured_logging("cli", state_root=tmp_path / "state")
    try:
        log_event(
            logging.getLogger("dialectic.cli"),
            logging.INFO,
            "diagnostic.recorded",
            detail="x" * 5000,
            count=2,
        )
    finally:
        close_structured_logging()

    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(row["detail"].encode("utf-8")) == 4096
    assert row["count"] == 2
