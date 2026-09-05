"""Transport-neutral lifecycle and bounded-input application boundary."""

from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .app_logging import log_event
from .audit import OfflineRunAuditor
from .config import ConfigError, ConfigLoader, decode_scalar_utf8, validate_mode
from .contracts import (
    ARTIFACT_SCHEMA_VERSION,
    MAX_DIAGNOSTIC_BYTES,
    CodeOutcome,
    ConsensusOutcome,
    FailureKind,
    RunMode,
    TOOL_VERSION,
)
from .redaction import (
    CredentialBoundaryError,
    KnownCredentials,
    RedactionError,
    redact_config,
)
from .schemas import (
    DialecticConfig,
    DoctorReport,
    RunAuditReport,
    RunRecord,
    SummaryRecord,
    WorkspaceRecord,
)
from .store import RunHandle, RunStore
from .turn_timing import TURN_EXTENSION_SECONDS, TurnDeadlineController


class RunExecutor(Protocol):
    async def __call__(self, context: "ExecutionContext") -> RunRecord: ...


class DoctorExecutor(Protocol):
    async def __call__(self, context: "DoctorContext") -> DoctorReport: ...


CredentialProvider = Callable[[DialecticConfig, RunMode], KnownCredentials]
ProgressObserver = Callable[[RunRecord], None]

_LOGGER = logging.getLogger(__name__)


def _empty_deadline_snapshot() -> dict[str, object]:
    return {
        "active": False,
        "turnCount": 0,
        "remainingSeconds": 0,
        "effectiveRemainingSeconds": 0,
        "canExtend": False,
        "turns": [],
    }


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    handle: RunHandle
    config: DialecticConfig
    input_text: str
    repository_path: Path | None
    credentials: KnownCredentials
    service: "DialecticService"
    turn_deadlines: TurnDeadlineController


@dataclass(frozen=True, slots=True)
class DoctorContext:
    mode: RunMode
    config: DialecticConfig
    config_sha256: str
    credentials: KnownCredentials
    store: RunStore
    tool_version: str


class DialecticFailure(RuntimeError):
    def __init__(self, kind: FailureKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class WorkflowTimedOut(RuntimeError):
    pass


class DialecticService:
    def __init__(
        self,
        store: RunStore,
        *,
        config_loader: ConfigLoader | None = None,
        credential_provider: CredentialProvider | None = None,
        code_executor: RunExecutor | None = None,
        council_executor: RunExecutor | None = None,
        doctor_executor: DoctorExecutor | None = None,
    ) -> None:
        self.store = store
        self._config_loader = config_loader or ConfigLoader()
        self._credential_provider = credential_provider or (lambda _config, _mode: KnownCredentials())
        self._executors: dict[RunMode, RunExecutor | None] = {
            "code": code_executor,
            "council": council_executor,
        }
        self._doctor_executor = doctor_executor
        self._progress_observer: ProgressObserver | None = None
        self._deadline_lock = threading.RLock()
        self._active_deadlines: dict[str, TurnDeadlineController] = {}

    def set_progress_observer(self, observer: ProgressObserver | None) -> None:
        """Set the non-authoritative presentation observer for durable transitions."""
        self._progress_observer = observer

    async def doctor(self, *, config_bytes: bytes, mode: RunMode) -> DoctorReport:
        """Qualify configured native targets without creating a workflow run."""

        loaded = self._config_loader.load(config_bytes, mode=mode)
        credentials = self._credential_provider(loaded.config, mode)
        credentials.validate_stream_limits(
            stdout_bytes=loaded.config.limits.max_agent_stdout_bytes,
            stderr_bytes=loaded.config.limits.max_agent_stderr_bytes,
        )
        if self._doctor_executor is None:
            raise RuntimeError("native doctor is not configured")
        return await self._doctor_executor(
            DoctorContext(
                mode=mode,
                config=loaded.config,
                config_sha256=loaded.source_sha256,
                credentials=credentials,
                store=self.store,
                tool_version=TOOL_VERSION,
            )
        )

    def audit_run(self, run_id: str) -> RunAuditReport:
        """Return a bounded, non-mutating integrity report for retained evidence."""

        return OfflineRunAuditor(self.store).audit(run_id)

    def turn_deadline_snapshot(self, run_id: str) -> dict[str, object]:
        """Return a presentation-only snapshot of controller-owned turn deadlines."""

        with self._deadline_lock:
            controller = self._active_deadlines.get(run_id)
        return controller.snapshot() if controller is not None else _empty_deadline_snapshot()

    def extend_turn_deadlines(self, run_id: str) -> dict[str, object]:
        """Extend active logical turns without weakening their hard ceiling."""

        with self._deadline_lock:
            controller = self._active_deadlines.get(run_id)
        if controller is None:
            raise ValueError("no active turn is available to extend")
        snapshot = controller.extend_active()
        if snapshot["extendedTurns"] == 0:
            raise ValueError("no active turn is eligible for extension")
        log_event(
            _LOGGER,
            logging.INFO,
            "turn.deadline_extended",
            run_id=run_id,
            seconds=int(TURN_EXTENSION_SECONDS),
            extended_turns=snapshot["extendedTurns"],
        )
        return snapshot

    def create_run(self, mode: RunMode) -> RunHandle:
        handle = self.store.bootstrap_run(mode)
        log_event(
            _LOGGER,
            logging.INFO,
            "run.created",
            run_id=handle.run_id,
            mode=mode,
            status="CREATED",
        )
        return handle

    def fail_invalid_input(self, handle: RunHandle, bounded_error: str) -> RunRecord:
        if len(bounded_error.encode("utf-8")) > MAX_DIAGNOSTIC_BYTES:
            raise ValueError("invalid-input diagnostic exceeds 4096 UTF-8 bytes")
        return self.fail_run(handle, "INVALID_INPUT", bounded_error, phase="PREFLIGHT")

    async def execute_code_once(
        self,
        handle: RunHandle,
        *,
        config_bytes: bytes,
        task_bytes: bytes,
        repository_path: Path | str,
    ) -> RunRecord:
        return await self._execute(
            handle,
            mode="code",
            config_bytes=config_bytes,
            input_bytes=task_bytes,
            input_name="task.md",
            repository_path=Path(repository_path),
        )

    async def execute_council_once(
        self,
        handle: RunHandle,
        *,
        config_bytes: bytes,
        prompt_bytes: bytes,
    ) -> RunRecord:
        return await self._execute(
            handle,
            mode="council",
            config_bytes=config_bytes,
            input_bytes=prompt_bytes,
            input_name="prompt.md",
            repository_path=None,
        )

    async def _execute(
        self,
        handle: RunHandle,
        *,
        mode: RunMode,
        config_bytes: bytes,
        input_bytes: bytes,
        input_name: str,
        repository_path: Path | None,
    ) -> RunRecord:
        created = self.store.read_handle(handle)
        if created.mode != mode or created.status != "CREATED":
            raise ValueError("run handle does not identify a matching CREATED run")
        try:
            loaded = self._config_loader.load(config_bytes)
            validate_mode(loaded.config, mode)
            if len(input_bytes) > loaded.config.limits.max_input_bytes:
                raise ConfigError(
                    f"{input_name} byte count exceeds limits.max_input_bytes "
                    f"({len(input_bytes)} > {loaded.config.limits.max_input_bytes})"
                )
            input_text = decode_scalar_utf8(input_bytes, input_name)
        except ConfigError as exc:
            return self.fail_invalid_input(handle, _bounded_detail(str(exc)))

        try:
            credentials = self._credential_provider(loaded.config, mode)
            redacted_config = redact_config(
                loaded.config,
                source_sha256=loaded.source_sha256,
                credentials=credentials,
            )
            credentials.validate_stream_limits(
                stdout_bytes=loaded.config.limits.max_agent_stdout_bytes,
                stderr_bytes=loaded.config.limits.max_agent_stderr_bytes,
            )
        except CredentialBoundaryError as exc:
            return self.fail_run(handle, "PREFLIGHT_FAILED", str(exc), phase="PREFLIGHT")
        except RedactionError as exc:
            return self.fail_run(handle, "INTERNAL_ERROR", str(exc), phase="PREFLIGHT")

        self.store.write_artifact(handle, "input/config.redacted.json", redacted_config)
        persisted_input = credentials.redact_bytes(input_text.encode("utf-8"))
        self.store.write_artifact(handle, f"input/{input_name}", persisted_input)
        self.start_run(handle, phase="PREFLIGHT")
        executor = self._executors[mode]
        if executor is None:
            return self.fail_run(
                handle,
                "PREFLIGHT_FAILED",
                "no native workflow executor is configured in Slice 0",
                phase="PREFLIGHT",
            )
        turn_deadlines = TurnDeadlineController()
        context = ExecutionContext(
            handle=handle,
            config=loaded.config,
            input_text=input_text,
            repository_path=repository_path,
            credentials=credentials,
            service=self,
            turn_deadlines=turn_deadlines,
        )
        with self._deadline_lock:
            self._active_deadlines[handle.run_id] = turn_deadlines
        try:
            result = await executor(context)
        except DialecticFailure as exc:
            return self.fail_run(handle, exc.kind, exc.detail)
        except WorkflowTimedOut as exc:
            return self.timeout_run(handle, str(exc))
        except asyncio.CancelledError:
            return self.cancel_run(handle, "user cancellation")
        except Exception as exc:
            stack = " <- ".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            )
            log_event(
                _LOGGER,
                logging.ERROR,
                "controller.unexpected_error",
                exception_type=type(exc).__name__,
                stack=stack,
            )
            return self.fail_run(
                handle,
                "INTERNAL_ERROR",
                f"unexpected controller error: {type(exc).__name__}",
            )
        finally:
            with self._deadline_lock:
                if self._active_deadlines.get(handle.run_id) is turn_deadlines:
                    self._active_deadlines.pop(handle.run_id, None)
        if result.run_id != handle.run_id or result.status not in {
            "FINALIZED",
            "FAILED",
            "TIMED_OUT",
            "CANCELLED",
        }:
            return self.fail_run(
                handle,
                "INTERNAL_ERROR",
                "workflow executor did not persist a matching terminal record",
            )
        return result

    def start_run(self, handle: RunHandle, *, phase: str) -> RunRecord:
        previous = self.store.read_handle(handle)
        now = datetime.now(UTC)
        record = previous.model_copy(
            update={"status": "RUNNING", "phase": phase, "updated_at": now}
        )
        record = RunRecord.model_validate(record.model_dump())
        self._persist_with_event(handle, record, "phase-started", {"phase": phase})
        return record

    def advance_phase(self, handle: RunHandle, phase: str) -> RunRecord:
        previous = self.store.read_handle(handle)
        record = previous.model_copy(update={"phase": phase, "updated_at": datetime.now(UTC)})
        record = RunRecord.model_validate(record.model_dump())
        self._persist_with_event(handle, record, "phase-started", {"phase": phase})
        return record

    def mark_model_work_started(self, handle: RunHandle) -> RunRecord:
        previous = self.store.read_handle(handle)
        now = datetime.now(UTC)
        record = previous.model_copy(update={"started_model_work_at": now, "updated_at": now})
        record = RunRecord.model_validate(record.model_dump())
        self.store.write_run(handle, record)
        log_event(
            _LOGGER,
            logging.INFO,
            "run.model_work_started",
            run_id=record.run_id,
            mode=record.mode,
            phase=record.phase,
            status=record.status,
        )
        return record

    def finalize_code(
        self,
        handle: RunHandle,
        outcome: CodeOutcome,
        *,
        unresolved_items: Sequence[str] = (),
        artifact_paths: dict[str, str] | None = None,
        markdown_notes: Sequence[str] = (),
    ) -> RunRecord:
        return self._finalize(
            handle,
            code_outcome=outcome,
            consensus_outcome=None,
            unresolved_items=unresolved_items,
            artifact_paths=artifact_paths,
            markdown_notes=markdown_notes,
        )

    def finalize_council(
        self,
        handle: RunHandle,
        outcome: ConsensusOutcome,
        *,
        unresolved_items: Sequence[str] = (),
        artifact_paths: dict[str, str] | None = None,
        markdown_notes: Sequence[str] = (),
    ) -> RunRecord:
        return self._finalize(
            handle,
            code_outcome=None,
            consensus_outcome=outcome,
            unresolved_items=unresolved_items,
            artifact_paths=artifact_paths,
            markdown_notes=markdown_notes,
        )

    def _finalize(
        self,
        handle: RunHandle,
        *,
        code_outcome: CodeOutcome | None,
        consensus_outcome: ConsensusOutcome | None,
        unresolved_items: Sequence[str],
        artifact_paths: dict[str, str] | None,
        markdown_notes: Sequence[str],
    ) -> RunRecord:
        previous = self.store.read_handle(handle)
        now = datetime.now(UTC)
        record = previous.model_copy(
            update={
                "status": "FINALIZED",
                "phase": "REPORTING",
                "code_outcome": code_outcome,
                "consensus_outcome": consensus_outcome,
                "failure_kind": None,
                "failure_detail": None,
                "updated_at": now,
                "completed_at": now,
            }
        )
        record = RunRecord.model_validate(record.model_dump())
        self._persist_with_event(handle, record, "run-finalized", {})
        self._persist_terminal_summary(
            handle,
            record,
            unresolved_items=unresolved_items,
            artifact_paths=artifact_paths,
            markdown_notes=markdown_notes,
        )
        return record

    def fail_run(
        self,
        handle: RunHandle,
        kind: FailureKind,
        detail: str,
        *,
        phase: str | None = None,
    ) -> RunRecord:
        previous = self.store.read_handle(handle)
        safe_detail = _bounded_detail(detail)
        now = datetime.now(UTC)
        record = previous.model_copy(
            update={
                "status": "FAILED",
                "phase": phase or previous.phase or "PREFLIGHT",
                "code_outcome": None,
                "consensus_outcome": None,
                "failure_kind": kind,
                "failure_detail": safe_detail,
                "updated_at": now,
                "completed_at": now,
            }
        )
        record = RunRecord.model_validate(record.model_dump())
        self._persist_with_event(handle, record, "run-failed", {"failure_kind": kind})
        self._persist_terminal_summary(handle, record)
        return record

    def timeout_run(self, handle: RunHandle, detail: str) -> RunRecord:
        return self._stop_without_failure(handle, "TIMED_OUT", detail)

    def cancel_run(self, handle: RunHandle, detail: str) -> RunRecord:
        return self._stop_without_failure(handle, "CANCELLED", detail)

    def _stop_without_failure(self, handle: RunHandle, status: str, detail: str) -> RunRecord:
        previous = self.store.read_handle(handle)
        now = datetime.now(UTC)
        record = previous.model_copy(
            update={
                "status": status,
                "phase": previous.phase or "PREFLIGHT",
                "code_outcome": None,
                "consensus_outcome": None,
                "failure_kind": None,
                "failure_detail": _bounded_detail(detail),
                "updated_at": now,
                "completed_at": now,
            }
        )
        record = RunRecord.model_validate(record.model_dump())
        self._persist_with_event(handle, record, f"run-{status.lower()}", {})
        self._persist_terminal_summary(handle, record)
        return record

    def get_run(self, run_id: str) -> RunRecord:
        return self.store.read_run(run_id)

    def get_result(self, run_id: str) -> SummaryRecord:
        record = self.get_run(run_id)
        if record.status in {"FINALIZED", "FAILED", "TIMED_OUT", "CANCELLED"}:
            return self.store.read_summary(run_id)
        outcome = record.code_outcome or record.consensus_outcome
        return SummaryRecord(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            run_id=record.run_id,
            mode=record.mode,
            status=record.status,
            outcome=outcome,
            failure_kind=record.failure_kind,
            unresolved_items=[],
            artifact_paths={"run": "run.json"},
        )

    def get_workspace(self, run_id: str) -> WorkspaceRecord | None:
        self.get_run(run_id)
        return self.store.read_workspace(run_id)

    def run_artifact_directory(self, run_id: str) -> Path:
        self.get_run(run_id)
        return (self.store.runs_root / run_id).resolve(strict=True)

    def _persist_with_event(
        self,
        handle: RunHandle,
        record: RunRecord,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        self.store.write_run(handle, record)
        self.store.append_event(
            handle,
            phase=record.phase,
            event_type=event_type,
            payload=payload,
        )
        fields: dict[str, str | None] = {
            "run_id": record.run_id,
            "mode": record.mode,
            "phase": record.phase,
            "status": record.status,
        }
        if record.failure_kind is not None:
            fields["failure_kind"] = record.failure_kind
        if record.failure_detail is not None:
            fields["failure_detail"] = record.failure_detail
        log_event(
            _LOGGER,
            logging.WARNING if record.status in {"FAILED", "TIMED_OUT"} else logging.INFO,
            event_type.replace("-", "."),
            **fields,
        )
        observer = self._progress_observer
        if observer is not None:
            try:
                observer(record)
            except Exception:
                # Terminal rendering is deliberately outside workflow authority.
                _LOGGER.warning("progress observer failed", exc_info=True)

    def _persist_terminal_summary(
        self,
        handle: RunHandle,
        record: RunRecord,
        *,
        unresolved_items: Sequence[str] = (),
        artifact_paths: dict[str, str] | None = None,
        markdown_notes: Sequence[str] = (),
    ) -> None:
        paths = {"events": "events.jsonl", "run": "run.json"}
        if (self.store.assert_handle(handle) / "git" / "workspace.json").is_file():
            paths["workspace"] = "git/workspace.json"
        if artifact_paths:
            paths.update(artifact_paths)
        summary = SummaryRecord(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            run_id=record.run_id,
            mode=record.mode,
            status=record.status,
            outcome=record.code_outcome or record.consensus_outcome,
            failure_kind=record.failure_kind,
            unresolved_items=list(unresolved_items),
            artifact_paths=paths,
        )
        self.store.write_artifact(handle, "summary.json", summary)
        lines = [f"# Dialectic run {record.run_id}", "", f"Status: {record.status}"]
        outcome = record.code_outcome or record.consensus_outcome
        if outcome is not None:
            lines.append(f"Outcome: {outcome}")
        if record.failure_kind is not None:
            lines.append(f"Failure: {record.failure_kind}")
        if unresolved_items:
            lines.extend(["", "Unresolved findings:"])
            lines.extend(f"- {item}" for item in unresolved_items)
        if markdown_notes:
            lines.extend(["", *markdown_notes])
        self.store.write_artifact(
            handle,
            "summary.md",
            ("\n".join(lines) + "\n").encode("utf-8"),
        )


def _bounded_detail(detail: str) -> str:
    raw = detail.encode("utf-8")[:MAX_DIAGNOSTIC_BYTES]
    return raw.decode("utf-8", errors="ignore")
