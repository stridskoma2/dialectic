"""Transport-neutral lifecycle and bounded-input application boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence

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
from .schemas import DialecticConfig, EventRecord, RunRecord, SummaryRecord
from .store import RunHandle, RunStore


class RunExecutor(Protocol):
    async def __call__(self, context: "ExecutionContext") -> RunRecord: ...


CredentialProvider = Callable[[DialecticConfig, RunMode], KnownCredentials]


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    handle: RunHandle
    config: DialecticConfig
    input_text: str
    repository_path: Path | None
    credentials: KnownCredentials
    service: "DialecticService"


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
    ) -> None:
        self.store = store
        self._config_loader = config_loader or ConfigLoader()
        self._credential_provider = credential_provider or (lambda _config, _mode: KnownCredentials())
        self._executors: dict[RunMode, RunExecutor | None] = {
            "code": code_executor,
            "council": council_executor,
        }

    def create_run(self, mode: RunMode) -> RunHandle:
        return self.store.bootstrap_run(mode)

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
            return self.fail_invalid_input(handle, str(exc))

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
        context = ExecutionContext(
            handle=handle,
            config=loaded.config,
            input_text=input_text,
            repository_path=repository_path,
            credentials=credentials,
            service=self,
        )
        try:
            result = await executor(context)
        except DialecticFailure as exc:
            return self.fail_run(handle, exc.kind, exc.detail)
        except WorkflowTimedOut as exc:
            return self.timeout_run(handle, str(exc))
        except asyncio.CancelledError:
            return self.cancel_run(handle, "user cancellation")
        except Exception as exc:
            return self.fail_run(
                handle,
                "INTERNAL_ERROR",
                f"unexpected controller error: {type(exc).__name__}",
            )
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

    def finalize_council(self, handle: RunHandle, outcome: ConsensusOutcome) -> RunRecord:
        return self._finalize(
            handle,
            code_outcome=None,
            consensus_outcome=outcome,
            unresolved_items=(),
            artifact_paths=None,
            markdown_notes=(),
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
        sequence = self.store.next_event_sequence(handle)
        self.store.append_event(
            handle,
            EventRecord(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                sequence=sequence,
                timestamp=datetime.now(UTC),
                run_id=handle.run_id,
                phase=record.phase,
                event_type=event_type,
                payload=payload,
            ),
        )

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
