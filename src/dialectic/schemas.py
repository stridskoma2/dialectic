"""Strict configuration, model payload, and version-one artifact schemas."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .contracts import (
    CODE_PHASES,
    COUNCIL_PHASES,
    CodeOutcome,
    ConsensusOutcome,
    FailureKind,
    RunMode,
    RunPhase,
    RunStatus,
    TurnPhase,
)

MODEL_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,127}$")
TARGET_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
PROCESS_UNIT_ID_RE = re.compile(r"^[a-z2-7]{16}$")


class ClosedModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _nonempty(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty after trimming")
    return value


def _nonempty_items(values: list[str], field_name: str) -> list[str]:
    for index, value in enumerate(values):
        _nonempty(value, f"{field_name}.{index}")
    return values


def _validate_model_selector(value: str) -> str:
    if not MODEL_SELECTOR_RE.fullmatch(value):
        raise ValueError("model must be 1..128 characters and match the model-selector grammar")
    return value


def _validate_target_id(value: str) -> str:
    if not TARGET_ID_RE.fullmatch(value):
        raise ValueError("id must match [a-z][a-z0-9-]{0,31}")
    return value


class AgentTarget(ClosedModel):
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None

    _model_selector = field_validator("model")(_validate_model_selector)

    @field_validator("effort")
    @classmethod
    def effort_is_bounded(cls, value: str | None) -> str | None:
        if value is not None:
            _nonempty(value, "effort")
            if len(value) > 65_536:
                raise ValueError("effort must contain at most 65536 Unicode scalar values")
        return value


class ReviewerSpec(ClosedModel):
    id: str
    target: Literal["@driver"] | None = None
    runtime: Literal["codex", "claude-code", "grok-build"] | None = None
    model: str | None = None
    effort: str | None = None
    lens: str

    _id_grammar = field_validator("id")(_validate_target_id)

    @field_validator("model")
    @classmethod
    def optional_model_selector(cls, value: str | None) -> str | None:
        return _validate_model_selector(value) if value is not None else None

    @field_validator("effort", "lens")
    @classmethod
    def nonempty_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None:
            _nonempty(value, info.field_name)
        return value

    @model_validator(mode="after")
    def target_is_exclusive(self) -> Self:
        if self.target == "@driver":
            if self.runtime is not None or self.model is not None or self.effort is not None:
                raise ValueError("an @driver reviewer cannot specify runtime, model, or effort")
        elif self.runtime is None or self.model is None:
            raise ValueError("a concrete reviewer must specify runtime and model")
        elif self.target is not None:
            raise ValueError("a concrete reviewer cannot specify target")
        return self


class ParticipantSpec(ClosedModel):
    id: str
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None

    _id_grammar = field_validator("id")(_validate_target_id)
    _model_selector = field_validator("model")(_validate_model_selector)

    @field_validator("effort")
    @classmethod
    def optional_effort_nonempty(cls, value: str | None) -> str | None:
        return _nonempty(value, "effort") if value is not None else None


class ModeratorSpec(ClosedModel):
    runtime: Literal["codex", "claude-code", "grok-build"]
    model: str
    effort: str | None = None

    _model_selector = field_validator("model")(_validate_model_selector)

    @field_validator("effort")
    @classmethod
    def optional_effort_nonempty(cls, value: str | None) -> str | None:
        return _nonempty(value, "effort") if value is not None else None


class DriverSpec(ClosedModel):
    runtime: Literal["codex"]
    model: str
    effort: str | None = None

    _model_selector = field_validator("model")(_validate_model_selector)

    @field_validator("effort")
    @classmethod
    def optional_effort_nonempty(cls, value: str | None) -> str | None:
        return _nonempty(value, "effort") if value is not None else None


class ConsensusSpec(ClosedModel):
    max_dissenters: int = Field(ge=0)


class CouncilSpec(ClosedModel):
    participants: list[ParticipantSpec] = Field(min_length=2, max_length=5)
    moderator: ModeratorSpec
    moderator_mode: Literal["fresh", "independent-opening"] = "fresh"
    consensus: ConsensusSpec

    @model_validator(mode="after")
    def participant_contract(self) -> Self:
        ids = [participant.id for participant in self.participants]
        if len(ids) != len(set(ids)):
            raise ValueError("council participant ids must be unique")
        if self.consensus.max_dissenters >= len(self.participants):
            raise ValueError(
                "consensus.max_dissenters "
                f"({self.consensus.max_dissenters}) must be less than participant count "
                f"({len(self.participants)})"
            )
        return self


class LimitsSpec(ClosedModel):
    max_reviewers: int = Field(ge=1, le=5)
    max_findings_per_reviewer: int = Field(ge=1, le=100)
    max_total_findings: int = Field(ge=1, le=500)
    max_council_participants: int = Field(ge=2, le=5)
    max_propositions: int = Field(ge=1, le=20)
    max_config_bytes: int = Field(ge=1, le=262_144)
    max_input_bytes: int = Field(ge=1, le=262_144)
    max_diff_bytes: int = Field(ge=1, le=1_048_576)
    max_changed_paths: int = Field(ge=1, le=10_000)
    max_changed_regular_file_bytes: int = Field(ge=1, le=67_108_864)
    max_candidate_change_bytes: int = Field(ge=1, le=268_435_456)
    max_packet_bytes: int = Field(ge=1, le=1_572_864)
    max_lens_chars: int = Field(ge=1, le=8_192)
    max_model_field_chars: int = Field(ge=1, le=65_536)
    max_model_list_items: int = Field(ge=1, le=500)
    max_agent_stdout_bytes: int = Field(ge=256, le=67_108_864)
    max_agent_stderr_bytes: int = Field(ge=256, le=16_777_216)
    max_turn_scratch_bytes: int = Field(ge=1, le=1_073_741_824)
    max_turn_scratch_entries: int = Field(ge=1, le=100_000)
    max_turn_scratch_depth: int = Field(ge=1, le=256)
    preflight_seconds: int = Field(ge=1, le=300)
    capability_probe_seconds: int = Field(ge=1, le=600)
    agent_turn_seconds: int = Field(ge=1, le=3_600)
    code_run_seconds: int = Field(ge=1, le=14_400)
    council_run_seconds: int = Field(ge=1, le=14_400)
    graceful_kill_seconds: int = Field(ge=1, le=30)
    turn_cleanup_seconds: int = Field(ge=1, le=300)
    code_review_cycles: Literal[1]
    council_discussion_rounds: Literal[1]

    @model_validator(mode="after")
    def cross_limits(self) -> Self:
        representable = min(
            self.max_reviewers * self.max_findings_per_reviewer,
            self.max_model_list_items,
        )
        if self.max_total_findings > representable:
            raise ValueError(
                "max_total_findings must be no greater than max_reviewers * "
                "max_findings_per_reviewer and max_model_list_items"
            )
        if self.max_changed_regular_file_bytes > self.max_candidate_change_bytes:
            raise ValueError(
                "max_changed_regular_file_bytes must be no greater than "
                "max_candidate_change_bytes"
            )
        return self


class DialecticConfig(ClosedModel):
    version: Literal[1]
    driver: DriverSpec | None = None
    reviewers: list[ReviewerSpec] | None = None
    council: CouncilSpec | None = None
    limits: LimitsSpec

    @model_validator(mode="after")
    def present_sections_obey_controller_limits(self) -> Self:
        limits = self.limits
        if self.reviewers is not None:
            if not 1 <= len(self.reviewers) <= 5:
                raise ValueError("reviewers must contain between 1 and 5 entries")
            if len(self.reviewers) > limits.max_reviewers:
                raise ValueError("reviewer count exceeds limits.max_reviewers")
            ids = [reviewer.id for reviewer in self.reviewers]
            if len(ids) != len(set(ids)):
                raise ValueError("reviewer ids must be unique")
            for index, reviewer in enumerate(self.reviewers):
                if len(reviewer.lens) > limits.max_lens_chars:
                    raise ValueError(
                        f"reviewers.{index}.lens exceeds limits.max_lens_chars"
                    )
        if self.council is not None:
            if len(self.council.participants) > limits.max_council_participants:
                raise ValueError(
                    "council participant count exceeds limits.max_council_participants"
                )
        for path, value in self._model_facing_strings():
            if len(value) > limits.max_model_field_chars:
                raise ValueError(f"{path} exceeds limits.max_model_field_chars")
        return self

    def _model_facing_strings(self) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        if self.driver is not None:
            values.extend((f"driver.{name}", value) for name, value in _spec_strings(self.driver))
        for index, reviewer in enumerate(self.reviewers or []):
            values.extend(
                (f"reviewers.{index}.{name}", value) for name, value in _spec_strings(reviewer)
            )
        if self.council is not None:
            for index, participant in enumerate(self.council.participants):
                values.extend(
                    (f"council.participants.{index}.{name}", value)
                    for name, value in _spec_strings(participant)
                )
            values.extend(
                (f"council.moderator.{name}", value)
                for name, value in _spec_strings(self.council.moderator)
            )
        return values


def _spec_strings(spec: BaseModel) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in spec.model_dump().items()
        if isinstance(value, str)
    ]


class ControllerArtifact(ClosedModel):
    artifact_schema_version: Literal[1]
    tool_version: str


class RunRecord(ControllerArtifact):
    run_id: str
    mode: RunMode
    status: RunStatus
    phase: RunPhase | None
    code_outcome: CodeOutcome | None
    consensus_outcome: ConsensusOutcome | None
    failure_kind: FailureKind | None
    failure_detail: str | None
    created_at: datetime
    updated_at: datetime
    started_model_work_at: datetime | None
    completed_at: datetime | None

    @field_validator("created_at", "updated_at", "started_model_work_at", "completed_at")
    @classmethod
    def timezone_aware(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{info.field_name} must be timezone-aware")
        return value

    @model_validator(mode="after")
    def lifecycle_contract(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.phase is None:
            if self.status != "CREATED":
                raise ValueError("phase=null is valid only while status is CREATED")
        else:
            permitted = CODE_PHASES if self.mode == "code" else COUNCIL_PHASES
            if self.phase not in permitted:
                raise ValueError(f"phase {self.phase} is invalid for {self.mode} mode")

        if self.status == "CREATED":
            self._require_absent(outcomes=True, failure=True, completion=True)
            if self.started_model_work_at is not None:
                raise ValueError("CREATED cannot have started_model_work_at")
        elif self.status == "RUNNING":
            self._require_absent(outcomes=True, failure=True, completion=True)
        elif self.status == "FINALIZED":
            if self.failure_kind is not None or self.failure_detail is not None:
                raise ValueError("FINALIZED cannot carry failure fields")
            if self.completed_at is None:
                raise ValueError("FINALIZED requires completed_at")
            if self.mode == "code":
                if self.code_outcome is None or self.consensus_outcome is not None:
                    raise ValueError("FINALIZED code run requires only code_outcome")
            elif self.consensus_outcome is None or self.code_outcome is not None:
                raise ValueError("FINALIZED council run requires only consensus_outcome")
        elif self.status == "FAILED":
            if self.failure_kind is None:
                raise ValueError("FAILED requires failure_kind")
            if self.failure_detail is None or not self.failure_detail.strip():
                raise ValueError("FAILED requires a concrete failure_detail")
            if self.code_outcome is not None or self.consensus_outcome is not None:
                raise ValueError("FAILED cannot carry a product outcome")
            if self.completed_at is None:
                raise ValueError("FAILED requires completed_at")
        elif self.status in {"TIMED_OUT", "CANCELLED"}:
            if any((self.code_outcome, self.consensus_outcome, self.failure_kind)):
                raise ValueError(f"{self.status} cannot carry outcome or failure_kind")
            if self.completed_at is None:
                raise ValueError(f"{self.status} requires completed_at")
        return self

    def _require_absent(self, *, outcomes: bool, failure: bool, completion: bool) -> None:
        if outcomes and (self.code_outcome is not None or self.consensus_outcome is not None):
            raise ValueError(f"{self.status} cannot carry a product outcome")
        if failure and (self.failure_kind is not None or self.failure_detail is not None):
            raise ValueError(f"{self.status} cannot carry failure fields")
        if completion and self.completed_at is not None:
            raise ValueError(f"{self.status} cannot have completed_at")


class EventRecord(ControllerArtifact):
    sequence: int = Field(ge=1)
    timestamp: datetime
    run_id: str
    phase: RunPhase | None
    event_type: str
    payload: dict[str, Any]

    @field_validator("timestamp")
    @classmethod
    def timestamp_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


class WorkspaceRecord(ControllerArtifact):
    repo_common_dir: str
    repo_filesystem_identity: str
    repo_lock_identity_sha256: str
    original_worktree: str
    original_branch: str | None
    base_sha: str
    dialectic_branch: str | None
    dialectic_worktree: str | None
    review_sha: str | None
    final_sha: str | None
    initial_diff_sha256: str | None
    repair_delta_sha256: str | None
    final_diff_sha256: str | None


class ReviewManifest(ControllerArtifact):
    base_sha: str
    review_sha: str
    diff_sha256: str
    reviewer_aliases: list[str]
    reports: list[str]


class ReviewFinding(ClosedModel):
    id: str
    severity: Literal["critical", "major", "minor", "nit"]
    category: str
    file: str | None
    line: int | None = Field(default=None, ge=1)
    claim: str
    evidence: str
    suggested_fix: str | None

    @field_validator("id")
    @classmethod
    def finding_id(cls, value: str) -> str:
        _nonempty(value, "id")
        if len(value) > 64:
            raise ValueError("finding id must contain at most 64 characters")
        return value

    @field_validator("category", "claim", "evidence")
    @classmethod
    def required_text(cls, value: str, info: ValidationInfo) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("suggested_fix")
    @classmethod
    def optional_fix_nonempty(cls, value: str | None) -> str | None:
        return _nonempty(value, "suggested_fix") if value is not None else None


class ReviewReport(ClosedModel):
    schema_version: Literal[1]
    base_sha: str
    head_sha: str
    verdict: Literal["pass", "changes_requested"]
    summary: str
    findings: list[ReviewFinding]

    @field_validator("summary")
    @classmethod
    def summary_nonempty(cls, value: str) -> str:
        return _nonempty(value, "summary")

    @model_validator(mode="after")
    def verdict_matches_findings(self, info: ValidationInfo) -> Self:
        if self.verdict == "pass" and self.findings:
            raise ValueError("pass requires an empty findings list")
        if self.verdict == "changes_requested" and not self.findings:
            raise ValueError("changes_requested requires at least one finding")
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding ids must be unique within a report")
        context = info.context or {}
        if len(self.findings) > context.get("max_findings", len(self.findings)):
            raise ValueError("findings exceed configured max_findings_per_reviewer")
        expected_base = context.get("base_sha")
        expected_head = context.get("head_sha")
        if expected_base is not None and self.base_sha != expected_base:
            raise ValueError("base_sha does not match the packet")
        if expected_head is not None and self.head_sha != expected_head:
            raise ValueError("head_sha does not match the packet")
        return self


class NormalizedFinding(ClosedModel):
    finding_key: str
    reviewer_alias: str
    source_finding_id: str
    finding: ReviewFinding


class FeedbackArtifact(ControllerArtifact):
    review_sha: str
    findings: list[NormalizedFinding]


class FindingDisposition(ClosedModel):
    finding_key: str
    outcome: Literal["fixed", "rejected_with_evidence", "not_fixed"]
    explanation: str

    @field_validator("finding_key", "explanation")
    @classmethod
    def required_disposition_text(cls, value: str, info: ValidationInfo) -> str:
        return _nonempty(value, info.field_name)


class DriverRepairReport(ClosedModel):
    schema_version: Literal[1]
    summary: str
    dispositions: list[FindingDisposition]

    @field_validator("summary")
    @classmethod
    def repair_summary_nonempty(cls, value: str) -> str:
        return _nonempty(value, "summary")

    @model_validator(mode="after")
    def disposition_keys_are_exact(self, info: ValidationInfo) -> Self:
        keys = [disposition.finding_key for disposition in self.dispositions]
        if len(keys) != len(set(keys)):
            raise ValueError("disposition finding keys must be unique")
        expected = (info.context or {}).get("finding_keys")
        if expected is not None and set(keys) != set(expected):
            raise ValueError("dispositions must contain every supplied finding key exactly once")
        return self


class SummaryRecord(ControllerArtifact):
    run_id: str
    mode: RunMode
    status: RunStatus
    outcome: CodeOutcome | ConsensusOutcome | None
    failure_kind: FailureKind | None
    unresolved_items: list[str]
    artifact_paths: dict[str, str]

    @model_validator(mode="after")
    def status_and_mode_match(self) -> Self:
        if self.status == "FINALIZED":
            if self.outcome is None or self.failure_kind is not None:
                raise ValueError("FINALIZED summary requires outcome and no failure")
            code_values = {
                "COMPLETED_NO_FINDINGS",
                "COMPLETED_AFTER_REPAIR",
                "COMPLETED_WITH_REBUTTALS",
                "COMPLETED_WITH_UNRESOLVED_FINDINGS",
            }
            if (self.outcome in code_values) != (self.mode == "code"):
                raise ValueError("summary outcome does not match mode")
        elif self.status == "FAILED":
            if self.failure_kind is None or self.outcome is not None:
                raise ValueError("FAILED summary requires failure and no outcome")
        elif self.outcome is not None or self.failure_kind is not None:
            raise ValueError("non-terminal/non-failed summary cannot carry outcome or failure")
        return self


class AliasMapArtifact(ControllerArtifact):
    aliases: dict[str, AgentTarget]


class RedactedConfigArtifact(ControllerArtifact):
    source_sha256: str
    normalized_config: DialecticConfig
    redacted_field_paths: list[str]

    @field_validator("redacted_field_paths")
    @classmethod
    def paths_are_sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("redacted_field_paths must be sorted and unique")
        return value


class TargetPreflightArtifact(ControllerArtifact):
    role: Literal["driver", "reviewer", "participant", "moderator"]
    target_id: str
    target: AgentTarget
    resolved_executable: str
    resolved_executable_identity: str
    resolved_executable_sha256: str
    spawned_root_executable: str
    spawned_root_identity: str
    spawned_root_sha256: str
    launch_kind: Literal["direct", "windows-batch-shim"]
    cli_version: str
    prompt_transport: Literal["stdin", "acp-stdio"]
    process_lifecycle: Literal["per-turn", "persistent-acp-session"]
    effective_static_flags: list[str]
    credential_env_names: list[str]
    denied_credential_path_sha256s: list[str]
    adapter_fixture_version: str
    capability_attestation_sha256: str
    authentication_verified: Literal[True]

    @model_validator(mode="after")
    def persistent_lifecycle_is_narrow(self) -> Self:
        if self.process_lifecycle == "persistent-acp-session" and (
            self.role != "participant"
            or self.prompt_transport != "acp-stdio"
        ):
            raise ValueError(
                "persistent lifecycle requires a participant ACP transport"
            )
        return self


class CapabilityProbeResult(ClosedModel):
    probe_id: str
    expected: Literal["allow", "deny"]
    observed: Literal["allowed", "denied", "unavailable"]
    passed: bool
    bounded_diagnostic: str | None

    @model_validator(mode="after")
    def result_is_consistent(self) -> Self:
        wanted = "allowed" if self.expected == "allow" else "denied"
        if self.passed != (self.observed == wanted):
            raise ValueError("passed must exactly reflect expected versus observed")
        return self


class DynamicFilesystemIdentity(ClosedModel):
    role: Literal[
        "isolated_worktree",
        "git_common_dir",
        "original_worktree",
        "state_root",
        "saved_auth_path",
        "os_temp_root",
        "outside_sentinel",
        "turn_scratch_root",
        "turn_scratch_control",
        "turn_scratch_tmp",
        "neutral_role_dir",
    ]
    path_sha256: str
    filesystem_identity: str


class CapabilityAttestationArtifact(ControllerArtifact):
    runtime: str
    executable_identity: str
    executable_sha256: str
    spawned_root_identity: str
    spawned_root_sha256: str
    cli_version: str
    platform_backend: str
    elevation_state: str
    adapter_fixture_version: str
    fixture_test_version: str
    profile_template_sha256: str
    managed_policy_sha256: str
    probe_results: list[CapabilityProbeResult]
    probe_results_sha256: str

    @model_validator(mode="after")
    def probe_set_is_closed(self, info: ValidationInfo) -> Self:
        ids = [result.probe_id for result in self.probe_results]
        if len(ids) != len(set(ids)):
            raise ValueError("probe ids must be unique")
        expected_ids = (info.context or {}).get("probe_ids")
        if expected_ids is not None and ids != list(expected_ids):
            raise ValueError("probe ids must exactly match fixture order")
        return self


class CapabilityBindingArtifact(ControllerArtifact):
    binding_id: str
    role: Literal["driver", "reviewer", "participant", "moderator"]
    target_id: str
    access_mode: Literal["driver-write", "packet-only"]
    target_preflight_artifact_sha256: str
    capability_attestation_sha256: str
    profile_template_sha256: str
    concrete_profile_sha256: str
    dynamic_filesystem_identities: list[DynamicFilesystemIdentity]
    canonical_instantiation_verified: Literal[True]

    @field_validator("dynamic_filesystem_identities")
    @classmethod
    def identities_are_sorted(cls, value: list[DynamicFilesystemIdentity]) -> list[DynamicFilesystemIdentity]:
        keys = [(item.role, item.path_sha256) for item in value]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("dynamic filesystem identities must be sorted and unique")
        return value

    @model_validator(mode="after")
    def identity_roles_match_access_mode(self) -> Self:
        roles = [identity.role for identity in self.dynamic_filesystem_identities]
        scratch_roles = {
            "turn_scratch_root",
            "turn_scratch_control",
            "turn_scratch_tmp",
        }
        if self.access_mode == "driver-write":
            if self.role != "driver":
                raise ValueError("driver-write bindings require the driver role")
            for scratch_role in scratch_roles:
                if roles.count(scratch_role) != 1:
                    raise ValueError(
                        f"driver binding requires exactly one {scratch_role} identity"
                    )
            if "neutral_role_dir" in roles:
                raise ValueError("driver binding cannot contain neutral_role_dir")
        else:
            if self.role == "driver":
                raise ValueError("packet-only bindings cannot use the driver role")
            if roles.count("neutral_role_dir") != 1:
                raise ValueError(
                    "packet-only binding requires exactly one neutral_role_dir identity"
                )
            forbidden = scratch_roles | {"isolated_worktree"}
            if forbidden.intersection(roles):
                raise ValueError(
                    "packet-only binding cannot contain scratch or isolated-worktree identities"
                )
        return self


class AgentRequestArtifact(ControllerArtifact):
    role: Literal["driver", "reviewer", "participant", "moderator"]
    target_id: str
    turn_phase: TurnPhase
    outbound_prompt_sha256: str
    persisted_prompt_sha256: str
    prompt: str
    output_schema: dict[str, Any] | None
    timeout_seconds: int = Field(gt=0)
    access_mode: Literal["driver-write", "packet-only"]


class StreamCaptureResult(ClosedModel):
    configured_limit_bytes: int = Field(gt=0)
    accepted_pre_redaction_bytes: int = Field(ge=0)
    accepted_pre_redaction_sha256: str
    discarded_guard_bytes: int = Field(ge=0)
    discarded_guard_reason: Literal["none", "overflow", "epoch-boundary"]
    truncated: bool
    persisted_bytes: int = Field(ge=0)
    persisted_sha256: str
    triggered_termination: bool

    @model_validator(mode="after")
    def counts_are_bounded(self) -> Self:
        if self.accepted_pre_redaction_bytes > self.configured_limit_bytes:
            raise ValueError("accepted bytes exceed configured stream limit")
        if self.persisted_bytes > self.configured_limit_bytes:
            raise ValueError("persisted bytes exceed configured stream limit")
        if self.discarded_guard_bytes > self.accepted_pre_redaction_bytes:
            raise ValueError("discarded guard exceeds accepted bytes")
        if self.triggered_termination and not self.truncated:
            raise ValueError("stream-triggered termination requires truncation")
        if (self.discarded_guard_reason == "none") != (
            self.discarded_guard_bytes == 0
        ):
            raise ValueError("guard reason none is equivalent to zero discarded bytes")
        if self.discarded_guard_reason == "overflow" and not (
            self.discarded_guard_bytes > 0
            and self.truncated
            and self.triggered_termination
        ):
            raise ValueError("overflow guard requires terminating truncation")
        if self.discarded_guard_reason == "epoch-boundary" and (
            self.discarded_guard_bytes == 0
            or self.truncated
            or self.triggered_termination
        ):
            raise ValueError("epoch-boundary guard requires a bounded non-final stream")
        return self


class AgentResponse(ControllerArtifact):
    runtime: str
    requested_model: str
    resolved_requested_model: str | None
    actual_model: str | None
    session_id: str | None
    text: str
    structured_output: dict[str, Any] | None
    usage: dict[str, Any] | None

    @field_validator("session_id")
    @classmethod
    def session_id_grammar(cls, value: str | None) -> str | None:
        if value is not None and not SESSION_ID_RE.fullmatch(value):
            raise ValueError("session_id violates the native argv grammar")
        return value


class TurnAttemptArtifact(ControllerArtifact):
    role: Literal["driver", "reviewer", "participant", "moderator"]
    target_id: str
    turn_phase: TurnPhase
    operation: Literal["start", "resume"]
    request_artifact_sha256: str
    target_preflight_artifact_sha256: str
    capability_binding_artifact_sha256: str
    started_at: datetime
    response_completed_at: datetime | None
    capture_completed_at: datetime
    process_origin: Literal[
        "none", "spawned-for-attempt", "retained-from-prior-turn"
    ]
    process_lifecycle: Literal["per-turn", "persistent-acp-session"]
    process_unit_id: str | None
    process_exit_code: int | None
    attempt_end_reason: Literal[
        "response-returned",
        "launch-failed",
        "agent-failed",
        "timeout",
        "cancelled",
        "output-limit",
        "peer-failure",
        "cleanup-failed",
    ]
    failure_kind: FailureKind | None
    process_disposition: Literal[
        "not-started", "retained-for-session", "closed", "cleanup-failed"
    ]
    stdout: StreamCaptureResult
    stderr: StreamCaptureResult
    response: AgentResponse | None
    bounded_diagnostic: str | None

    @model_validator(mode="after")
    def process_evidence_is_consistent(self) -> Self:
        if self.capture_completed_at < self.started_at:
            raise ValueError("capture_completed_at cannot precede started_at")
        if self.response_completed_at is not None and not (
            self.started_at <= self.response_completed_at <= self.capture_completed_at
        ):
            raise ValueError("response_completed_at must fall within the attempt")
        if self.process_origin == "none":
            if (
                self.process_unit_id is not None
                or self.process_exit_code is not None
                or self.response is not None
                or self.response_completed_at is not None
                or self.process_disposition != "not-started"
                or self.attempt_end_reason
                not in {"launch-failed", "peer-failure", "cancelled"}
            ):
                raise ValueError("an unstarted attempt has inconsistent process evidence")
            empty_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            for stream in (self.stdout, self.stderr):
                if (
                    stream.accepted_pre_redaction_bytes != 0
                    or stream.persisted_bytes != 0
                    or stream.accepted_pre_redaction_sha256 != empty_sha
                    or stream.persisted_sha256 != empty_sha
                ):
                    raise ValueError("an unstarted process requires exact zero-byte stream evidence")
        else:
            if self.process_unit_id is None or not PROCESS_UNIT_ID_RE.fullmatch(
                self.process_unit_id
            ):
                raise ValueError("owned process attempts require an opaque process-unit id")
            if self.process_disposition == "not-started":
                raise ValueError("an owned process cannot be not-started")
        if self.process_disposition == "not-started" and self.process_origin != "none":
            raise ValueError("not-started disposition requires origin none")
        if self.process_disposition == "retained-for-session":
            if not (
                self.process_lifecycle == "persistent-acp-session"
                and self.process_exit_code is None
                and self.response is not None
                and self.response_completed_at is not None
                and self.attempt_end_reason == "response-returned"
                and self.failure_kind is None
                and self.turn_phase in {"opening", "cross-examination"}
            ):
                raise ValueError("retained disposition violates the persistent ACP contract")
        if self.process_disposition == "closed" and self.process_exit_code is None:
            raise ValueError("closed process disposition requires an observed exit code")
        if self.process_disposition == "cleanup-failed" and not (
            self.failure_kind == "PROCESS_CLEANUP_FAILED"
            and self.attempt_end_reason == "cleanup-failed"
        ):
            raise ValueError("cleanup-failed disposition requires authoritative cleanup failure")
        if self.process_lifecycle == "per-turn" and self.process_origin != "none":
            if self.process_origin != "spawned-for-attempt" or self.process_disposition not in {
                "closed",
                "cleanup-failed",
            }:
                raise ValueError("per-turn process attempts must spawn and finalize one unit")
        if self.process_lifecycle == "persistent-acp-session":
            expected_origin = (
                "spawned-for-attempt"
                if self.turn_phase == "opening"
                else "retained-from-prior-turn"
            )
            if self.process_origin != "none" and self.process_origin != expected_origin:
                raise ValueError("persistent ACP origin mismatches its logical turn")
            if self.turn_phase == "ballot" and self.process_disposition == "retained-for-session":
                raise ValueError("ballot attempts cannot retain a persistent lease")
        if self.response is None and self.response_completed_at is not None:
            raise ValueError("response_completed_at requires a normalized response")
        if self.response is not None and self.response_completed_at is None:
            raise ValueError("a normalized response requires response_completed_at")
        if self.attempt_end_reason == "response-returned" and (
            self.response is None or self.failure_kind is not None
        ):
            raise ValueError("response-returned requires a successful normalized response")
        if (
            self.response is not None
            and self.attempt_end_reason == "response-returned"
            and self.process_disposition == "closed"
            and self.process_lifecycle == "per-turn"
            and self.process_exit_code != 0
        ):
            raise ValueError("successful per-turn response requires zero process exit")
        for stream in (self.stdout, self.stderr):
            if stream.discarded_guard_reason == "epoch-boundary" and not (
                self.process_lifecycle == "persistent-acp-session"
                and self.process_disposition == "retained-for-session"
            ):
                raise ValueError("epoch-boundary guard requires a retained ACP attempt")
            if stream.discarded_guard_reason == "overflow" and (
                self.attempt_end_reason != "output-limit"
            ):
                raise ValueError("overflow guard requires output-limit attempt end")
        return self


class ReviewReportArtifact(ControllerArtifact):
    reviewer_alias: str
    target: AgentTarget
    packet_sha256: str
    report: ReviewReport


class CouncilClaim(ClosedModel):
    statement: str
    evidence: str | None
    assumption: str | None

    @field_validator("statement")
    @classmethod
    def statement_nonempty(cls, value: str) -> str:
        return _nonempty(value, "statement")


class OpeningPosition(ClosedModel):
    schema_version: Literal[1]
    conclusion: str
    claims: list[CouncilClaim]
    uncertainties: list[str]
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("conclusion")
    @classmethod
    def conclusion_nonempty(cls, value: str) -> str:
        return _nonempty(value, "conclusion")

    @field_validator("uncertainties")
    @classmethod
    def uncertainties_nonempty(cls, value: list[str]) -> list[str]:
        return _nonempty_items(value, "uncertainties")


class CouncilRevision(ClosedModel):
    schema_version: Literal[1]
    strongest_opposing_claim: str
    critique: str
    changed_mind: bool
    change_reason: str | None
    revised_conclusion: str
    remaining_objections: list[str]

    @field_validator("strongest_opposing_claim", "critique", "revised_conclusion")
    @classmethod
    def required_revision_text(cls, value: str, info: ValidationInfo) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("remaining_objections")
    @classmethod
    def objections_nonempty(cls, value: list[str]) -> list[str]:
        return _nonempty_items(value, "remaining_objections")


class CandidateProposition(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    statement: str
    rationale: str
    supporting_participants: list[str]
    known_objections: list[str]

    _id_grammar = field_validator("id")(_validate_target_id)

    @field_validator("statement", "rationale")
    @classmethod
    def proposition_text_nonempty(cls, value: str, info: ValidationInfo) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("supporting_participants", "known_objections")
    @classmethod
    def proposition_list_items_nonempty(
        cls, value: list[str], info: ValidationInfo
    ) -> list[str]:
        return _nonempty_items(value, info.field_name)


class CandidateConclusion(ClosedModel):
    schema_version: Literal[1]
    answer: str
    propositions: list[CandidateProposition] = Field(min_length=1)
    unresolved_questions: list[str]

    @field_validator("answer")
    @classmethod
    def answer_nonempty(cls, value: str) -> str:
        return _nonempty(value, "answer")

    @field_validator("unresolved_questions")
    @classmethod
    def unresolved_items_nonempty(cls, value: list[str]) -> list[str]:
        return _nonempty_items(value, "unresolved_questions")

    @model_validator(mode="after")
    def proposition_contract(self, info: ValidationInfo) -> Self:
        context = info.context or {}
        maximum = context.get("max_propositions", 20)
        if len(self.propositions) > maximum:
            raise ValueError("candidate exceeds configured max_propositions")
        ids = [proposition.id for proposition in self.propositions]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate proposition ids must be unique")
        aliases = context.get("participant_aliases")
        if aliases is not None:
            permitted = set(aliases)
            for proposition in self.propositions:
                supporters = proposition.supporting_participants
                if len(supporters) != len(set(supporters)) or not set(supporters) <= permitted:
                    raise ValueError("supporting_participants must be unique known aliases")
        return self


class PropositionVote(ClosedModel):
    proposition_id: str
    vote: Literal["accept", "reject", "abstain"]
    reason: str

    @field_validator("proposition_id", "reason")
    @classmethod
    def vote_text_nonempty(cls, value: str, info: ValidationInfo) -> str:
        return _nonempty(value, info.field_name)


class CouncilBallot(ClosedModel):
    schema_version: Literal[1]
    proposition_votes: list[PropositionVote]
    blocking_objection: bool
    blocking_objection_evidence: str | None
    minority_report: str | None

    @model_validator(mode="after")
    def ballot_contract(self, info: ValidationInfo) -> Self:
        vote_ids = [vote.proposition_id for vote in self.proposition_votes]
        if len(vote_ids) != len(set(vote_ids)):
            raise ValueError("proposition votes must not contain duplicates")
        expected = (info.context or {}).get("proposition_ids")
        if expected is not None and set(vote_ids) != set(expected):
            raise ValueError("ballot proposition ids must exactly match the candidate")
        if self.blocking_objection:
            if self.blocking_objection_evidence is None or not self.blocking_objection_evidence.strip():
                raise ValueError("blocking objection requires non-empty evidence")
        elif self.blocking_objection_evidence is not None:
            raise ValueError("non-blocking ballot requires null blocking evidence")
        return self


def derive_overall_vote(ballot: CouncilBallot) -> Literal["accept", "reject", "abstain"]:
    if ballot.blocking_objection or any(vote.vote == "reject" for vote in ballot.proposition_votes):
        return "reject"
    if any(vote.vote == "abstain" for vote in ballot.proposition_votes):
        return "abstain"
    return "accept"


class DerivedBallot(ControllerArtifact):
    participant_alias: str
    ballot: CouncilBallot
    derived_overall_vote: Literal["accept", "reject", "abstain"]

    @model_validator(mode="after")
    def controller_derivation_matches(self) -> Self:
        if self.derived_overall_vote != derive_overall_vote(self.ballot):
            raise ValueError("derived_overall_vote does not match the ballot")
        return self


class OpeningPositionArtifact(ControllerArtifact):
    participant_alias: str
    packet_sha256: str
    position: OpeningPosition


class ModeratorOpeningArtifact(ControllerArtifact):
    moderator_target: AgentTarget
    packet_sha256: str
    position: OpeningPosition


class CouncilRevisionArtifact(ControllerArtifact):
    participant_alias: str
    packet_sha256: str
    revision: CouncilRevision


class CandidateConclusionArtifact(ControllerArtifact):
    moderator_target: AgentTarget
    packet_sha256: str
    candidate: CandidateConclusion


class AgentRequest(ClosedModel):
    role: Literal["driver", "reviewer", "participant", "moderator"]
    target_id: str
    turn_phase: TurnPhase
    prompt: str
    output_schema: dict[str, Any] | None
    timeout_seconds: int = Field(gt=0)
    working_directory: str
    access_mode: Literal["driver-write", "packet-only"]


class PreflightResult(ClosedModel):
    target: AgentTarget
    requested_model: str
    resolved_requested_model: str | None
    actual_model: str | None
    authentication_verified: bool
