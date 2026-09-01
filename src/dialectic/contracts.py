"""Closed version-one lifecycle and exit-code contracts."""

from __future__ import annotations

from typing import Final, Literal, TypeAlias

ARTIFACT_SCHEMA_VERSION: Final = 1
TOOL_VERSION: Final = "0.1.0"
RUN_ID_PATTERN: Final = r"^[0-9]{8}T[0-9]{6}Z-[a-z2-7]{10}$"
MAX_NAMED_INPUT_BYTES: Final = 262_144
MAX_DIAGNOSTIC_BYTES: Final = 4_096
TRUNCATION_MARKER: Final = b"<dialectic:truncated>\n"
REDACTION_MARKER: Final = b"[REDACT]"
MAX_JSON_DEPTH: Final = 64
MAX_ARG_BYTES: Final = 4_096

RunMode: TypeAlias = Literal["code", "council"]
ResearchMode: TypeAlias = Literal["offline", "live-web"]
RunStatus: TypeAlias = Literal[
    "CREATED", "RUNNING", "FINALIZED", "FAILED", "TIMED_OUT", "CANCELLED"
]
CodePhase: TypeAlias = Literal[
    "PREFLIGHT",
    "WORKTREE_SETUP",
    "DRIVER_INITIAL",
    "INITIAL_VALIDATION",
    "REVIEWERS",
    "FEEDBACK",
    "DRIVER_REPAIR",
    "FINAL_VALIDATION",
    "REPORTING",
]
CouncilPhase: TypeAlias = Literal[
    "PREFLIGHT",
    "OPENING_POSITIONS",
    "CROSS_EXAMINATION",
    "MODERATION",
    "BALLOTS",
    "REPORTING",
]
RunPhase: TypeAlias = CodePhase | CouncilPhase
TurnPhase: TypeAlias = Literal[
    "initial", "repair", "review", "opening", "cross-examination", "candidate", "ballot"
]
SessionCloseReason: TypeAlias = Literal[
    "completed", "phase-failure", "workflow-timeout", "cancelled"
]
CodeOutcome: TypeAlias = Literal[
    "COMPLETED_NO_FINDINGS",
    "COMPLETED_AFTER_REPAIR",
    "COMPLETED_WITH_REBUTTALS",
    "COMPLETED_WITH_UNRESOLVED_FINDINGS",
]
ConsensusOutcome: TypeAlias = Literal["UNANIMOUS", "ROUGH_CONSENSUS", "CONTESTED"]
FailureKind: TypeAlias = Literal[
    "INVALID_INPUT",
    "PREFLIGHT_FAILED",
    "REPOSITORY_BUSY",
    "UNSUPPORTED_REPOSITORY",
    "DRIVER_FAILED",
    "NO_CHANGES",
    "UNSUPPORTED_CHANGE",
    "DIFF_TOO_LARGE",
    "PACKET_TOO_LARGE",
    "AGENT_OUTPUT_TOO_LARGE",
    "MODEL_MISMATCH",
    "REVIEW_FAILED",
    "REPAIR_FAILED",
    "NO_QUORUM",
    "MODERATOR_FAILED",
    "PROCESS_CLEANUP_FAILED",
    "STATE_CORRUPT",
    "INTERNAL_ERROR",
]

FAILURE_KINDS: Final[tuple[FailureKind, ...]] = (
    "INVALID_INPUT",
    "PREFLIGHT_FAILED",
    "REPOSITORY_BUSY",
    "UNSUPPORTED_REPOSITORY",
    "DRIVER_FAILED",
    "NO_CHANGES",
    "UNSUPPORTED_CHANGE",
    "DIFF_TOO_LARGE",
    "PACKET_TOO_LARGE",
    "AGENT_OUTPUT_TOO_LARGE",
    "MODEL_MISMATCH",
    "REVIEW_FAILED",
    "REPAIR_FAILED",
    "NO_QUORUM",
    "MODERATOR_FAILED",
    "PROCESS_CLEANUP_FAILED",
    "STATE_CORRUPT",
    "INTERNAL_ERROR",
)

CODE_PHASES: Final = (
    "PREFLIGHT",
    "WORKTREE_SETUP",
    "DRIVER_INITIAL",
    "INITIAL_VALIDATION",
    "REVIEWERS",
    "FEEDBACK",
    "DRIVER_REPAIR",
    "FINAL_VALIDATION",
    "REPORTING",
)
COUNCIL_PHASES: Final = (
    "PREFLIGHT",
    "OPENING_POSITIONS",
    "CROSS_EXAMINATION",
    "MODERATION",
    "BALLOTS",
    "REPORTING",
)

FAILURE_EXIT_CODES: Final[dict[FailureKind, int]] = {
    "INVALID_INPUT": 2,
    "PREFLIGHT_FAILED": 2,
    "REPOSITORY_BUSY": 2,
    "UNSUPPORTED_REPOSITORY": 2,
    "DRIVER_FAILED": 3,
    "NO_CHANGES": 3,
    "UNSUPPORTED_CHANGE": 3,
    "DIFF_TOO_LARGE": 3,
    "PACKET_TOO_LARGE": 3,
    "AGENT_OUTPUT_TOO_LARGE": 3,
    "MODEL_MISMATCH": 3,
    "REVIEW_FAILED": 3,
    "REPAIR_FAILED": 3,
    "NO_QUORUM": 3,
    "MODERATOR_FAILED": 3,
    "PROCESS_CLEANUP_FAILED": 3,
    "STATE_CORRUPT": 3,
    "INTERNAL_ERROR": 3,
}


def exit_code_for(status: RunStatus, failure_kind: FailureKind | None = None) -> int:
    """Return the frozen command exit code for a terminal record."""

    if status == "FINALIZED":
        return 0
    if status == "TIMED_OUT":
        return 4
    if status == "CANCELLED":
        return 130
    if status == "FAILED" and failure_kind is not None:
        return FAILURE_EXIT_CODES[failure_kind]
    if status in {"CREATED", "RUNNING"}:
        return 0
    raise ValueError("failed status requires a failure kind")
