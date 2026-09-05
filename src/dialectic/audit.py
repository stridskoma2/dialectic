"""Bounded, read-only validation of retained Dialectic run evidence."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from pydantic import BaseModel

from .contracts import CODE_PHASES, COUNCIL_PHASES, TOOL_VERSION
from .schemas import (
    AgentRequestArtifact,
    AliasMapArtifact,
    CandidateConclusionArtifact,
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    CouncilRevisionArtifact,
    DerivedBallot,
    EventRecord,
    FeedbackArtifact,
    ModeratorOpeningArtifact,
    OpeningPositionArtifact,
    RedactedConfigArtifact,
    ReviewManifest,
    ReviewReportArtifact,
    RunAuditIssue,
    RunAuditReport,
    RunRecord,
    SummaryRecord,
    TargetPreflightArtifact,
    TurnAttemptArtifact,
    WebSourceCitationArtifact,
    WorkspaceRecord,
)
from .store import RunNotFoundError, RunStore, canonical_json_bytes, validate_run_id

MAX_AUDIT_FILES = 100_000
MAX_AUDIT_TOTAL_BYTES = 2_147_483_648
MAX_AUDIT_FILE_BYTES = 134_217_728
MAX_AUDIT_JSON_BYTES = 4_194_304
MAX_AUDIT_EVENTS_BYTES = 16_777_216
MAX_AUDIT_ISSUES = 256

_TERMINAL_EVENT = {
    "FINALIZED": "run-finalized",
    "FAILED": "run-failed",
    "TIMED_OUT": "run-timed_out",
    "CANCELLED": "run-cancelled",
}
_TERMINAL_STATUSES = frozenset(_TERMINAL_EVENT)
_TARGET_PATH = re.compile(
    r"^audit/targets/(?P<role>driver|reviewer|participant|moderator)/"
    r"(?P<target>[a-z][a-z0-9-]{0,31})\.json$"
)
_BINDING_PATH = re.compile(
    r"^audit/capabilities/(?P<role>driver|reviewer|participant|moderator)/"
    r"(?P<target>[a-z][a-z0-9-]{0,31})/"
    r"(?P<phase>[a-z][a-z-]{0,31})\.binding\.json$"
)
_TURN_PATH = re.compile(
    r"^turns/(?P<role>driver|reviewer|participant|moderator)/"
    r"(?P<target>[a-z][a-z0-9-]{0,31})/"
    r"(?P<phase>initial|repair|review|opening|cross-examination|candidate|ballot)"
    r"\.(?P<kind>request|attempt)\.json$"
)


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    relative_path: str
    size: int
    sha256: str
    data: bytes | None


@dataclass(slots=True)
class _AuditState:
    run_id: str
    files: dict[str, _FileSnapshot] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)
    models: dict[str, BaseModel] = field(default_factory=dict)
    issues: list[RunAuditIssue] = field(default_factory=list)
    bytes_checked: int = 0
    events_checked: int = 0
    attempts_checked: int = 0
    manifest_entries: list[tuple[str, int, str]] = field(default_factory=list)

    def issue(
        self,
        code: str,
        detail: str,
        *,
        path: str | None = None,
        severity: str = "error",
    ) -> None:
        if len(self.issues) >= MAX_AUDIT_ISSUES:
            if self.issues[-1].code != "ISSUE_LIMIT_REACHED":
                self.issues[-1] = RunAuditIssue(
                    severity="error",
                    code="ISSUE_LIMIT_REACHED",
                    path=None,
                    detail="audit stopped retaining individual issues at its bounded limit",
                )
            return
        self.issues.append(
            RunAuditIssue(
                severity=severity,
                code=code,
                path=_bounded_path(path),
                detail=_bounded_detail(detail),
            )
        )


class OfflineRunAuditor:
    """Inspect a run without repairing it or writing to its evidence tree."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def audit(self, run_id: str) -> RunAuditReport:
        validate_run_id(run_id)
        run_dir = self.store.runs_root / run_id
        if not run_dir.exists():
            raise RunNotFoundError(f"run not found: {run_id}")

        state = _AuditState(run_id)
        self._snapshot_run_tree(run_dir, state)
        self._parse_artifacts(state)
        record = self._run_record(state)
        events = self._audit_events(state, record)
        self._audit_summary(state, record)
        self._audit_capability_evidence(state)
        self._audit_attempts(state, record)
        self._audit_hash_sidecars(state)
        self._audit_workspace(state)
        self._audit_review_graph(state)
        self._audit_council_graph(state)
        self._audit_required_evidence(state, record)
        self._audit_run_lifecycle(state, record, events)

        valid = not any(issue.severity == "error" for issue in state.issues)
        status = record.status if record is not None else None
        complete = valid and status in _TERMINAL_STATUSES
        manifest = None
        if state.manifest_entries:
            digest = hashlib.sha256()
            for path, size, sha256 in sorted(state.manifest_entries):
                digest.update(f"{path}\0{size}\0{sha256}\n".encode("utf-8"))
            manifest = digest.hexdigest()
        return RunAuditReport(
            tool_version=TOOL_VERSION,
            run_id=run_id,
            valid=valid,
            complete=complete,
            status=status,
            files_checked=len(state.files),
            bytes_checked=state.bytes_checked,
            events_checked=state.events_checked,
            attempts_checked=state.attempts_checked,
            manifest_sha256=manifest,
            issues=state.issues,
        )

    def _snapshot_run_tree(self, run_dir: Path, state: _AuditState) -> None:
        try:
            root_info = run_dir.lstat()
        except OSError as exc:
            state.issue("RUN_DIRECTORY_UNREADABLE", str(exc))
            return
        if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            state.issue("RUN_DIRECTORY_UNSAFE", "run path is not a non-reparse directory")
            return
        self._audit_permissions(".", root_info, state)

        pending = [(run_dir, "")]
        visited_directories = 0
        while pending:
            directory, prefix = pending.pop()
            visited_directories += 1
            if visited_directories > MAX_AUDIT_FILES:
                state.issue("FILE_COUNT_LIMIT", "run directory traversal exceeded its entry bound")
                return
            try:
                entries = os.scandir(directory)
            except OSError as exc:
                state.issue("DIRECTORY_UNREADABLE", str(exc), path=prefix or ".")
                continue
            try:
                for entry in entries:
                    relative = f"{prefix}/{entry.name}" if prefix else entry.name
                    entry_path = Path(entry.path)
                    try:
                        # Windows DirEntry.stat() can report zero identity fields on
                        # some Python builds; Path.lstat() preserves the full values.
                        info = entry_path.lstat()
                    except OSError as exc:
                        state.issue("ARTIFACT_UNREADABLE", str(exc), path=relative)
                        continue
                    if _is_link_or_reparse(info):
                        state.issue(
                            "LINK_OR_REPARSE_ARTIFACT",
                            "links and reparse points are not valid retained evidence",
                            path=relative,
                        )
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        self._audit_permissions(relative, info, state)
                        state.directories.add(relative)
                        pending.append((entry_path, relative))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        state.issue(
                            "UNSUPPORTED_ARTIFACT_TYPE",
                            "retained evidence must contain only regular files and directories",
                            path=relative,
                        )
                        continue
                    if len(state.files) >= MAX_AUDIT_FILES:
                        state.issue("FILE_COUNT_LIMIT", "run contains too many artifact files")
                        return
                    self._audit_permissions(relative, info, state)
                    if info.st_nlink != 1:
                        state.issue(
                            "HARD_LINKED_ARTIFACT",
                            "retained artifact has more than one hard link",
                            path=relative,
                        )
                    if info.st_size > MAX_AUDIT_FILE_BYTES:
                        state.issue(
                            "ARTIFACT_SIZE_LIMIT",
                            f"artifact exceeds the {MAX_AUDIT_FILE_BYTES}-byte audit ceiling",
                            path=relative,
                        )
                        continue
                    if state.bytes_checked + info.st_size > MAX_AUDIT_TOTAL_BYTES:
                        state.issue(
                            "TOTAL_SIZE_LIMIT",
                            f"run evidence exceeds the {MAX_AUDIT_TOTAL_BYTES}-byte audit ceiling",
                        )
                        return
                    snapshot = self._read_snapshot(entry_path, relative, info, state)
                    if snapshot is not None:
                        state.files[relative] = snapshot
                        state.bytes_checked += snapshot.size
                        state.manifest_entries.append(
                            (snapshot.relative_path, snapshot.size, snapshot.sha256)
                        )
            except OSError as exc:
                state.issue("DIRECTORY_UNREADABLE", str(exc), path=prefix or ".")
            finally:
                entries.close()

    @staticmethod
    def _audit_permissions(
        path: str,
        info: os.stat_result,
        state: _AuditState,
    ) -> None:
        if os.name == "nt":
            return
        if stat.S_IMODE(info.st_mode) & 0o077:
            state.issue(
                "INSECURE_PERMISSIONS",
                "retained evidence is accessible outside its owner",
                path=str(path),
            )

    @staticmethod
    def _read_snapshot(
        path: Path,
        relative: str,
        expected: os.stat_result,
        state: _AuditState,
    ) -> _FileSnapshot | None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            state.issue("ARTIFACT_UNREADABLE", str(exc), path=relative)
            return None
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != expected.st_dev
                or opened.st_ino != expected.st_ino
                or opened.st_size != expected.st_size
            ):
                state.issue(
                    "ARTIFACT_CHANGED_DURING_AUDIT",
                    "artifact identity or size changed while it was opened",
                    path=relative,
                )
                return None
            digest = hashlib.sha256()
            retained = bytearray() if _retain_bytes(relative, opened.st_size) else None
            observed = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > expected.st_size:
                    state.issue(
                        "ARTIFACT_CHANGED_DURING_AUDIT",
                        "artifact grew while it was being read",
                        path=relative,
                    )
                    return None
                digest.update(chunk)
                if retained is not None:
                    retained.extend(chunk)
            if observed != expected.st_size:
                state.issue(
                    "ARTIFACT_CHANGED_DURING_AUDIT",
                    "artifact size changed while it was being read",
                    path=relative,
                )
                return None
            return _FileSnapshot(
                path=path,
                relative_path=relative,
                size=observed,
                sha256=digest.hexdigest(),
                data=bytes(retained) if retained is not None else None,
            )
        except OSError as exc:
            state.issue("ARTIFACT_UNREADABLE", str(exc), path=relative)
            return None
        finally:
            os.close(descriptor)

    def _parse_artifacts(self, state: _AuditState) -> None:
        for relative, snapshot in state.files.items():
            if not relative.endswith(".json"):
                continue
            model_type = _artifact_model(relative)
            if model_type is None:
                state.issue(
                    "UNRECOGNIZED_JSON_ARTIFACT",
                    "no current artifact schema is registered for this JSON file",
                    path=relative,
                    severity="warning",
                )
                continue
            if snapshot.data is None:
                state.issue(
                    "JSON_SIZE_LIMIT",
                    f"JSON artifact exceeds the {MAX_AUDIT_JSON_BYTES}-byte parsing ceiling",
                    path=relative,
                )
                continue
            try:
                model = model_type.model_validate_json(snapshot.data, strict=True)
            except Exception as exc:
                state.issue("SCHEMA_INVALID", str(exc), path=relative)
                continue
            state.models[relative] = model
            if canonical_json_bytes(model) != snapshot.data:
                state.issue(
                    "NONCANONICAL_JSON",
                    "artifact is schema-valid but not in canonical persisted form",
                    path=relative,
                )

    @staticmethod
    def _run_record(state: _AuditState) -> RunRecord | None:
        if "run.json" not in state.files:
            state.issue("RUN_RECORD_MISSING", "run.json is absent", path="run.json")
            return None
        model = state.models.get("run.json")
        if not isinstance(model, RunRecord):
            return None
        if model.run_id != state.run_id:
            state.issue("RUN_ID_MISMATCH", "run.json does not identify its directory", path="run.json")
        return model

    def _audit_events(
        self, state: _AuditState, record: RunRecord | None
    ) -> list[EventRecord]:
        snapshot = state.files.get("events.jsonl")
        if snapshot is None:
            if record is not None and record.status != "CREATED":
                state.issue("EVENT_LOG_MISSING", "events.jsonl is absent", path="events.jsonl")
            return []
        if snapshot.size > MAX_AUDIT_EVENTS_BYTES or snapshot.data is None:
            state.issue(
                "EVENT_LOG_SIZE_LIMIT",
                f"events.jsonl exceeds the {MAX_AUDIT_EVENTS_BYTES}-byte audit ceiling",
                path="events.jsonl",
            )
            return []
        if snapshot.data and not snapshot.data.endswith(b"\n"):
            state.issue("EVENT_LOG_PARTIAL_LINE", "events.jsonl lacks a final newline", path="events.jsonl")
        events: list[EventRecord] = []
        prior_timestamp = None
        phases = CODE_PHASES if record is not None and record.mode == "code" else COUNCIL_PHASES
        prior_phase_index = -1
        for sequence, line in enumerate(snapshot.data.splitlines(keepends=True), start=1):
            if len(line) > MAX_AUDIT_JSON_BYTES:
                state.issue("EVENT_LINE_SIZE_LIMIT", "event line exceeds its parsing ceiling", path="events.jsonl")
                continue
            try:
                event = EventRecord.model_validate_json(line, strict=True)
            except Exception as exc:
                state.issue("EVENT_SCHEMA_INVALID", str(exc), path="events.jsonl")
                continue
            events.append(event)
            if canonical_json_bytes(event) != line:
                state.issue("NONCANONICAL_EVENT", f"event {sequence} is not canonical JSON", path="events.jsonl")
            if event.sequence != sequence:
                state.issue("EVENT_SEQUENCE_GAP", f"expected sequence {sequence}, found {event.sequence}", path="events.jsonl")
            if event.run_id != state.run_id:
                state.issue("EVENT_RUN_ID_MISMATCH", f"event {sequence} names another run", path="events.jsonl")
            if prior_timestamp is not None and event.timestamp < prior_timestamp:
                state.issue("EVENT_TIME_REGRESSION", f"event {sequence} precedes the prior event", path="events.jsonl")
            prior_timestamp = event.timestamp
            if event.phase is None or event.phase not in phases:
                state.issue("EVENT_PHASE_INVALID", f"event {sequence} has an invalid phase", path="events.jsonl")
            else:
                phase_index = phases.index(event.phase)
                if phase_index < prior_phase_index:
                    state.issue("EVENT_PHASE_REGRESSION", f"event {sequence} regresses the run phase", path="events.jsonl")
                prior_phase_index = max(prior_phase_index, phase_index)
            if not event.event_type.strip():
                state.issue("EVENT_TYPE_EMPTY", f"event {sequence} has an empty type", path="events.jsonl")
        state.events_checked = len(events)
        return events

    @staticmethod
    def _audit_summary(state: _AuditState, record: RunRecord | None) -> None:
        if record is None:
            return
        terminal = record.status in _TERMINAL_STATUSES
        summary = state.models.get("summary.json")
        if terminal and not isinstance(summary, SummaryRecord):
            state.issue("SUMMARY_MISSING", "terminal run lacks a valid summary.json", path="summary.json")
            return
        if not terminal and "summary.json" in state.files:
            state.issue("PREMATURE_SUMMARY", "non-terminal run contains summary.json", path="summary.json")
            return
        if not isinstance(summary, SummaryRecord):
            return
        expected_outcome = record.code_outcome or record.consensus_outcome
        if (
            summary.run_id != record.run_id
            or summary.mode != record.mode
            or summary.status != record.status
            or summary.outcome != expected_outcome
            or summary.failure_kind != record.failure_kind
        ):
            state.issue("SUMMARY_RUN_MISMATCH", "summary.json does not match run.json", path="summary.json")
        for name, relative in summary.artifact_paths.items():
            if not _safe_relative_path(relative):
                state.issue("SUMMARY_PATH_UNSAFE", f"summary path {name!r} is not a safe relative path", path="summary.json")
            elif relative not in state.files and relative not in state.directories:
                state.issue("SUMMARY_ARTIFACT_MISSING", f"summary path {name!r} does not exist", path=relative)
        if terminal and "summary.md" not in state.files:
            state.issue("MARKDOWN_SUMMARY_MISSING", "terminal run lacks summary.md", path="summary.md")

    def _audit_capability_evidence(self, state: _AuditState) -> None:
        preflights: dict[str, tuple[str, TargetPreflightArtifact]] = {}
        bindings: dict[str, tuple[str, CapabilityBindingArtifact]] = {}
        for relative, model in state.models.items():
            snapshot = state.files[relative]
            if isinstance(model, TargetPreflightArtifact):
                preflights[snapshot.sha256] = (relative, model)
                match = _TARGET_PATH.fullmatch(relative)
                if match is None or (model.role, model.target_id) != (
                    match.group("role"), match.group("target")
                ):
                    state.issue("PREFLIGHT_PATH_MISMATCH", "target preflight fields do not match its path", path=relative)
                self._audit_attestation(state, model.capability_attestation_sha256)
            elif isinstance(model, CapabilityBindingArtifact):
                bindings[snapshot.sha256] = (relative, model)
                match = _BINDING_PATH.fullmatch(relative)
                if match is None or (model.role, model.target_id) != (
                    match.group("role"), match.group("target")
                ):
                    state.issue("BINDING_PATH_MISMATCH", "capability binding fields do not match its path", path=relative)

        for relative, binding in bindings.values():
            linked = preflights.get(binding.target_preflight_artifact_sha256)
            if linked is None:
                state.issue("PREFLIGHT_REFERENCE_MISSING", "binding references no retained target preflight", path=relative)
                continue
            _, preflight = linked
            if (binding.role, binding.target_id) != (preflight.role, preflight.target_id):
                state.issue("PREFLIGHT_REFERENCE_MISMATCH", "binding references another role or target", path=relative)
            if binding.capability_attestation_sha256 != preflight.capability_attestation_sha256:
                state.issue("ATTESTATION_REFERENCE_MISMATCH", "binding and preflight reference different attestations", path=relative)

    def _audit_attestation(self, state: _AuditState, sha256: str) -> None:
        relative = f"@capability-attestations/{sha256}.json"
        if any(path == relative for path, _, _ in state.manifest_entries):
            return
        path = self.store.capability_attestations_root / f"{sha256}.json"
        try:
            info = path.lstat()
        except OSError as exc:
            state.issue("ATTESTATION_MISSING", str(exc), path=relative)
            return
        if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            state.issue("ATTESTATION_UNSAFE", "capability attestation is not a single-link regular file", path=relative)
            return
        if info.st_size > MAX_AUDIT_JSON_BYTES:
            state.issue("ATTESTATION_SIZE_LIMIT", "capability attestation exceeds its audit ceiling", path=relative)
            return
        snapshot = self._read_snapshot(path, relative, info, state)
        if snapshot is None:
            return
        state.bytes_checked += snapshot.size
        state.manifest_entries.append((relative, snapshot.size, snapshot.sha256))
        if snapshot.sha256 != sha256:
            state.issue("ATTESTATION_HASH_MISMATCH", "capability attestation content does not match its reference", path=relative)
        try:
            artifact = CapabilityAttestationArtifact.model_validate_json(snapshot.data, strict=True)
        except Exception as exc:
            state.issue("ATTESTATION_SCHEMA_INVALID", str(exc), path=relative)
            return
        if canonical_json_bytes(artifact) != snapshot.data:
            state.issue("NONCANONICAL_ATTESTATION", "capability attestation is not canonical JSON", path=relative)

    @staticmethod
    def _audit_attempts(state: _AuditState, record: RunRecord | None) -> None:
        preflight_hashes = {
            state.files[path].sha256: model
            for path, model in state.models.items()
            if isinstance(model, TargetPreflightArtifact)
        }
        binding_hashes = {
            state.files[path].sha256: model
            for path, model in state.models.items()
            if isinstance(model, CapabilityBindingArtifact)
        }
        request_paths = {
            path for path, model in state.models.items() if isinstance(model, AgentRequestArtifact)
        }
        attempt_paths: set[str] = set()
        persistent: dict[tuple[str, str], list[TurnAttemptArtifact]] = {}
        for relative, model in state.models.items():
            if not isinstance(model, TurnAttemptArtifact):
                continue
            state.attempts_checked += 1
            attempt_paths.add(relative)
            match = _TURN_PATH.fullmatch(relative)
            if match is None or match.group("kind") != "attempt":
                state.issue("ATTEMPT_PATH_INVALID", "attempt artifact has an invalid path", path=relative)
                continue
            expected_identity = (match.group("role"), match.group("target"), match.group("phase"))
            if (model.role, model.target_id, model.turn_phase) != expected_identity:
                state.issue("ATTEMPT_PATH_MISMATCH", "attempt fields do not match its path", path=relative)
            root = relative[: -len(".attempt.json")]
            request_path = f"{root}.request.json"
            request = state.models.get(request_path)
            if not isinstance(request, AgentRequestArtifact):
                state.issue("ATTEMPT_REQUEST_MISSING", "attempt lacks a valid request artifact", path=request_path)
            else:
                if model.request_artifact_sha256 != state.files[request_path].sha256:
                    state.issue("REQUEST_HASH_MISMATCH", "attempt request hash does not match retained bytes", path=relative)
                if (request.role, request.target_id, request.turn_phase) != (
                    model.role, model.target_id, model.turn_phase
                ):
                    state.issue("REQUEST_ATTEMPT_MISMATCH", "request and attempt identify different turns", path=relative)
                if hashlib.sha256(request.prompt.encode("utf-8")).hexdigest() != request.persisted_prompt_sha256:
                    state.issue("PERSISTED_PROMPT_HASH_MISMATCH", "request prompt does not match its persisted hash", path=request_path)
            for stream_name, stream in (("stdout", model.stdout), ("stderr", model.stderr)):
                stream_path = f"{root}.{stream_name}.txt"
                snapshot = state.files.get(stream_path)
                if snapshot is None:
                    state.issue("STREAM_MISSING", f"attempt lacks {stream_name} evidence", path=stream_path)
                elif snapshot.size != stream.persisted_bytes or snapshot.sha256 != stream.persisted_sha256:
                    state.issue("STREAM_HASH_MISMATCH", f"{stream_name} evidence does not match the attempt", path=stream_path)
            preflight = preflight_hashes.get(model.target_preflight_artifact_sha256)
            if preflight is None:
                state.issue("ATTEMPT_PREFLIGHT_MISSING", "attempt references no retained target preflight", path=relative)
            elif (preflight.role, preflight.target_id) != (model.role, model.target_id):
                state.issue("ATTEMPT_PREFLIGHT_MISMATCH", "attempt references another role or target", path=relative)
            binding = binding_hashes.get(model.capability_binding_artifact_sha256)
            if binding is None:
                state.issue("ATTEMPT_BINDING_MISSING", "attempt references no retained capability binding", path=relative)
            elif (binding.role, binding.target_id) != (model.role, model.target_id):
                state.issue("ATTEMPT_BINDING_MISMATCH", "attempt references another role or target", path=relative)
            if model.process_lifecycle == "persistent-acp-session" and model.process_unit_id is not None:
                persistent.setdefault((model.role, model.target_id), []).append(model)

        terminal = record is not None and record.status in _TERMINAL_STATUSES
        for request_path in request_paths:
            attempt_path = request_path[: -len(".request.json")] + ".attempt.json"
            if attempt_path not in attempt_paths:
                state.issue(
                    "ORPHAN_REQUEST",
                    "request has no completed attempt evidence",
                    path=request_path,
                    severity="error" if terminal else "warning",
                )
        for key, attempts in persistent.items():
            order = {"opening": 0, "cross-examination": 1, "ballot": 2}
            attempts.sort(key=lambda attempt: order.get(attempt.turn_phase, 99))
            unit_ids = {attempt.process_unit_id for attempt in attempts}
            if len(unit_ids) != 1:
                state.issue("PERSISTENT_UNIT_MISMATCH", f"persistent target {key[1]} uses more than one process unit")
            positions = [order.get(attempt.turn_phase, 99) for attempt in attempts]
            if 99 in positions or len(positions) != len(set(positions)):
                state.issue("PERSISTENT_EPOCH_ORDER", f"persistent target {key[1]} has invalid epoch ordering")
            if any(
                later.started_at < earlier.started_at
                for earlier, later in zip(attempts, attempts[1:])
            ):
                state.issue("PERSISTENT_EPOCH_TIME_REGRESSION", f"persistent target {key[1]} has regressing epoch timestamps")
            if terminal and attempts[-1].process_disposition == "retained-for-session":
                state.issue("TERMINAL_LIVE_LEASE", f"terminal run retains the process for target {key[1]}")

    @staticmethod
    def _audit_hash_sidecars(state: _AuditState) -> None:
        for relative, snapshot in state.files.items():
            if not relative.endswith(".sha256"):
                continue
            if snapshot.data is None or not re.fullmatch(rb"[0-9a-f]{64}\n", snapshot.data):
                state.issue("HASH_SIDECAR_INVALID", "SHA-256 sidecar is not canonical", path=relative)
                continue
            if relative == "reviews/core.sha256":
                continue
            companion_path = relative[: -len(".sha256")]
            companion = state.files.get(companion_path)
            if companion is None:
                state.issue("HASHED_ARTIFACT_MISSING", "SHA-256 sidecar has no companion artifact", path=relative)
            elif snapshot.data != (companion.sha256 + "\n").encode("ascii"):
                state.issue("HASH_SIDECAR_MISMATCH", "SHA-256 sidecar does not match its companion", path=relative)

    @staticmethod
    def _audit_workspace(state: _AuditState) -> None:
        workspace = state.models.get("git/workspace.json")
        if not isinstance(workspace, WorkspaceRecord):
            return
        checks = (
            ("git/initial.diff", workspace.initial_diff_sha256),
            ("git/repair.delta.diff", workspace.repair_delta_sha256),
            ("git/final.diff", workspace.final_diff_sha256),
        )
        for relative, expected in checks:
            snapshot = state.files.get(relative)
            if expected is None:
                if snapshot is not None and relative == "git/repair.delta.diff":
                    state.issue("UNEXPECTED_REPAIR_DELTA", "workspace says no repair delta but its artifact exists", path=relative)
            elif snapshot is None:
                state.issue("WORKSPACE_ARTIFACT_MISSING", "workspace references a missing diff", path=relative)
            elif snapshot.sha256 != expected:
                state.issue("WORKSPACE_HASH_MISMATCH", "workspace diff hash does not match retained bytes", path=relative)

    @staticmethod
    def _audit_review_graph(state: _AuditState) -> None:
        manifest = state.models.get("reviews/manifest.json")
        workspace = state.models.get("git/workspace.json")
        if not isinstance(manifest, ReviewManifest):
            return
        if isinstance(workspace, WorkspaceRecord) and (
            manifest.base_sha != workspace.base_sha or manifest.review_sha != workspace.review_sha
        ):
            state.issue("REVIEW_MANIFEST_WORKSPACE_MISMATCH", "review manifest does not match the workspace", path="reviews/manifest.json")
        initial_diff = state.files.get("git/initial.diff")
        if initial_diff is not None and manifest.diff_sha256 != initial_diff.sha256:
            state.issue("REVIEW_DIFF_HASH_MISMATCH", "review manifest does not match the reviewed diff", path="reviews/manifest.json")
        expected_reports = {f"reviews/{alias}.json" for alias in manifest.reviewer_aliases}
        if set(manifest.reports) != expected_reports:
            state.issue("REVIEW_REPORT_SET_MISMATCH", "review manifest report paths are not exact", path="reviews/manifest.json")
        for relative in expected_reports:
            report = state.models.get(relative)
            if not isinstance(report, ReviewReportArtifact):
                state.issue("REVIEW_REPORT_MISSING", "review manifest references no valid report", path=relative)
                continue
            if report.reviewer_alias not in manifest.reviewer_aliases:
                state.issue("REVIEW_ALIAS_MISMATCH", "review report uses an unknown alias", path=relative)
            if report.report.base_sha != manifest.base_sha or report.report.head_sha != manifest.review_sha:
                state.issue("REVIEW_SHA_MISMATCH", "review report examined another Git state", path=relative)
        feedback = state.models.get("feedback.json")
        if isinstance(feedback, FeedbackArtifact) and feedback.review_sha != manifest.review_sha:
            state.issue("FEEDBACK_SHA_MISMATCH", "feedback does not identify the reviewed SHA", path="feedback.json")

    @staticmethod
    def _audit_council_graph(state: _AuditState) -> None:
        candidate = state.models.get("council/candidate.json")
        ballot_models = [
            (path, model)
            for path, model in state.models.items()
            if isinstance(model, DerivedBallot)
        ]
        if ballot_models and not isinstance(candidate, CandidateConclusionArtifact):
            state.issue("COUNCIL_CANDIDATE_MISSING", "ballots exist without a valid candidate", path="council/candidate.json")
            return
        if not isinstance(candidate, CandidateConclusionArtifact):
            return
        proposition_ids = {item.id for item in candidate.candidate.propositions}
        for path, ballot in ballot_models:
            vote_ids = {vote.proposition_id for vote in ballot.ballot.proposition_votes}
            if vote_ids != proposition_ids:
                state.issue("BALLOT_PROPOSITION_MISMATCH", "ballot does not cover the exact candidate propositions", path=path)

    @staticmethod
    def _audit_required_evidence(
        state: _AuditState, record: RunRecord | None
    ) -> None:
        if record is None or record.status != "FINALIZED":
            return
        required = {"input/config.redacted.json"}
        if record.mode == "code":
            required.update(
                {
                    "input/task.md",
                    "git/workspace.json",
                    "git/initial.diff",
                    "git/initial.diff.sha256",
                    "git/final.diff",
                    "git/final.diff.sha256",
                    "reviews/manifest.json",
                    "turns/driver/driver/initial.request.json",
                    "turns/driver/driver/initial.attempt.json",
                    "turns/driver/driver/initial.stdout.txt",
                    "turns/driver/driver/initial.stderr.txt",
                }
            )
            manifest = state.models.get("reviews/manifest.json")
            if isinstance(manifest, ReviewManifest):
                for alias in manifest.reviewer_aliases:
                    root = f"turns/reviewer/{alias}/review"
                    required.update(
                        {
                            f"{root}.request.json",
                            f"{root}.attempt.json",
                            f"{root}.stdout.txt",
                            f"{root}.stderr.txt",
                            f"reviews/{alias}.json",
                        }
                    )
            if record.code_outcome != "COMPLETED_NO_FINDINGS":
                root = "turns/driver/driver/repair"
                required.update(
                    {
                        "feedback.json",
                        f"{root}.request.json",
                        f"{root}.attempt.json",
                        f"{root}.stdout.txt",
                        f"{root}.stderr.txt",
                    }
                )
        else:
            required.update(
                {
                    "input/prompt.md",
                    "council/aliases.json",
                    "council/candidate.json",
                }
            )
            moderator_roots = ["turns/moderator/moderator/candidate"]
            redacted_config = state.models.get("input/config.redacted.json")
            if (
                isinstance(redacted_config, RedactedConfigArtifact)
                and redacted_config.normalized_config.council is not None
                and redacted_config.normalized_config.council.moderator_mode
                == "independent-opening"
            ):
                required.add("council/moderator-opening.json")
                moderator_roots.append("turns/moderator/moderator/opening")
            for root in moderator_roots:
                required.update(
                    {
                        f"{root}.request.json",
                        f"{root}.attempt.json",
                        f"{root}.stdout.txt",
                        f"{root}.stderr.txt",
                    }
                )
            aliases = state.models.get("council/aliases.json")
            if isinstance(aliases, AliasMapArtifact):
                for index, alias in enumerate(aliases.aliases):
                    target_id = f"participant-{chr(ord('a') + index)}"
                    required.update(
                        {
                            f"council/opening/{target_id}.json",
                            f"council/cross-examination/{target_id}.json",
                            f"council/ballots/{target_id}.json",
                        }
                    )
                    for phase in ("opening", "cross-examination", "ballot"):
                        root = f"turns/participant/{target_id}/{phase}"
                        required.update(
                            {
                                f"{root}.request.json",
                                f"{root}.attempt.json",
                                f"{root}.stdout.txt",
                                f"{root}.stderr.txt",
                            }
                        )
                    ballot = state.models.get(f"council/ballots/{target_id}.json")
                    if isinstance(ballot, DerivedBallot) and ballot.participant_alias != alias:
                        state.issue(
                            "COUNCIL_ALIAS_MISMATCH",
                            "ballot alias does not match the controller alias ledger",
                            path=f"council/ballots/{target_id}.json",
                        )
        for relative in sorted(required):
            if relative not in state.files:
                state.issue(
                    "FINALIZED_ARTIFACT_MISSING",
                    "finalized run lacks required workflow evidence",
                    path=relative,
                )

    @staticmethod
    def _audit_run_lifecycle(
        state: _AuditState,
        record: RunRecord | None,
        events: list[EventRecord],
    ) -> None:
        if record is None:
            return
        if record.status == "CREATED" and events:
            state.issue("CREATED_RUN_HAS_EVENTS", "a CREATED run must not have lifecycle events", path="events.jsonl")
        if record.status != "CREATED" and not events:
            return
        if events and record.phase != events[-1].phase:
            state.issue("RUN_EVENT_PHASE_MISMATCH", "run.json phase does not match the last event", path="events.jsonl")
        terminal_types = set(_TERMINAL_EVENT.values())
        observed_terminal = [event.event_type for event in events if event.event_type in terminal_types]
        if record.status in _TERMINAL_STATUSES:
            expected = _TERMINAL_EVENT[record.status]
            if observed_terminal != [expected] or not events or events[-1].event_type != expected:
                state.issue("TERMINAL_EVENT_MISMATCH", "terminal run lacks one matching final lifecycle event", path="events.jsonl")
        elif observed_terminal:
            state.issue("PREMATURE_TERMINAL_EVENT", "non-terminal run contains a terminal lifecycle event", path="events.jsonl")


def _artifact_model(relative: str) -> type[BaseModel] | None:
    exact: dict[str, type[BaseModel]] = {
        "run.json": RunRecord,
        "summary.json": SummaryRecord,
        "git/workspace.json": WorkspaceRecord,
        "input/config.redacted.json": RedactedConfigArtifact,
        "reviews/manifest.json": ReviewManifest,
        "feedback.json": FeedbackArtifact,
        "council/aliases.json": AliasMapArtifact,
        "council/moderator-opening.json": ModeratorOpeningArtifact,
        "council/candidate.json": CandidateConclusionArtifact,
    }
    if relative in exact:
        return exact[relative]
    if _TARGET_PATH.fullmatch(relative):
        return TargetPreflightArtifact
    if _BINDING_PATH.fullmatch(relative):
        return CapabilityBindingArtifact
    match = _TURN_PATH.fullmatch(relative)
    if match is not None:
        return AgentRequestArtifact if match.group("kind") == "request" else TurnAttemptArtifact
    if re.fullmatch(r"reviews/[a-z][a-z0-9-]{0,31}\.json", relative):
        return ReviewReportArtifact
    if re.fullmatch(r"council/opening/[a-z][a-z0-9-]{0,31}\.json", relative):
        return OpeningPositionArtifact
    if re.fullmatch(r"council/cross-examination/[a-z][a-z0-9-]{0,31}\.json", relative):
        return CouncilRevisionArtifact
    if re.fullmatch(r"council/ballots/[a-z][a-z0-9-]{0,31}\.json", relative):
        return DerivedBallot
    if re.fullmatch(
        r"research/sources/(reviewer|participant|moderator)/[a-z][a-z0-9-]{0,31}/"
        r"(review|opening|cross-examination|candidate|ballot)\.json",
        relative,
    ):
        return WebSourceCitationArtifact
    return None


def _retain_bytes(relative: str, size: int) -> bool:
    if relative == "events.jsonl":
        return size <= MAX_AUDIT_EVENTS_BYTES
    return (
        relative.endswith(".json") and size <= MAX_AUDIT_JSON_BYTES
    ) or (relative.endswith(".sha256") and size <= 1_024) or (
        relative == "summary.md" and size <= MAX_AUDIT_JSON_BYTES
    )


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value == path.as_posix()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _bounded_detail(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")[:4_096]
    return encoded.decode("utf-8", errors="ignore") or "audit check failed"


def _bounded_path(value: str | None) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8", errors="replace")[:4_096]
    return encoded.decode("utf-8", errors="ignore")


__all__ = ["OfflineRunAuditor"]
