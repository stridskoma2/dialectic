from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest
import yaml
from pydantic import ValidationError

from dialectic.adapters import AgentAdapter, ScriptedAgentAdapter, ScriptedStep
from dialectic.council_once import (
    CouncilOnceOrchestrator,
    _ballot_prompt,
    _cross_prompt,
    _opening_prompt,
)
from dialectic.native_adapters import NativePreflightError, NativeTurnError
from dialectic.schemas import (
    AgentResponse,
    AgentTarget,
    CapabilityBindingArtifact,
    CandidateConclusion,
    CouncilBallot,
    DialecticConfig,
    DynamicFilesystemIdentity,
    DerivedBallot,
    OpeningPosition,
    TurnAttemptArtifact,
)
from dialectic.service import DialecticService
from dialectic.store import RunStore, canonical_json_bytes
from dialectic.workflow_evidence import canonical_mapping_bytes


def _target(index: int) -> dict[str, str]:
    values = [
        {"runtime": "codex", "model": "codex-model"},
        {"runtime": "claude-code", "model": "claude-model"},
        {"runtime": "grok-build", "model": "grok-model"},
        {"runtime": "codex", "model": "codex-alt"},
        {"runtime": "claude-code", "model": "claude-alt"},
    ]
    return values[index]


def _opening(index: int, **updates: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "conclusion": f"Independent conclusion {index}",
        "claims": [
            {
                "statement": f"Claim {index}",
                "evidence": f"Evidence {index}",
                "assumption": None,
            }
        ],
        "uncertainties": [f"Uncertainty {index}"],
        "confidence": 0.75,
    }
    value.update(updates)
    return value


def _revision(index: int, **updates: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "strongest_opposing_claim": f"Opposition {index}",
        "critique": f"Critique {index}",
        "changed_mind": index == 1,
        "change_reason": "The opposing evidence was stronger" if index == 1 else None,
        "revised_conclusion": f"Revised conclusion {index}",
        "remaining_objections": [f"Objection {index}"],
    }
    value.update(updates)
    return value


def _candidate(count: int = 2, **updates: Any) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "answer": "Use the bounded candidate answer.",
        "propositions": [
            {
                "id": f"p-{index + 1}",
                "statement": f"Proposition {index + 1}",
                "rationale": f"Rationale {index + 1}",
                "supporting_participants": ["Participant A"],
                "known_objections": [],
            }
            for index in range(count)
        ],
        "unresolved_questions": ["One unresolved question"],
    }
    value.update(updates)
    return value


def _ballot(kind: str, proposition_ids: list[str]) -> dict[str, Any]:
    votes = "accept"
    blocker = False
    evidence = None
    minority = None
    if kind == "reject":
        votes = "reject"
        minority = "I reject the candidate."
    elif kind == "abstain":
        votes = "abstain"
        minority = "I cannot ratify yet."
    elif kind == "blocker":
        blocker = True
        evidence = "This creates a concrete safety failure."
        minority = "The blocker must be resolved."
    return {
        "schema_version": 1,
        "proposition_votes": [
            {"proposition_id": value, "vote": votes, "reason": f"Reason for {value}"}
            for value in proposition_ids
        ],
        "blocking_objection": blocker,
        "blocking_objection_evidence": evidence,
        "minority_report": minority,
    }


def _response(target: dict[str, str], session_id: str | None, payload: dict[str, Any]) -> AgentResponse:
    return AgentResponse(
        artifact_schema_version=1,
        tool_version="0.1.0",
        runtime=target["runtime"],
        requested_model=target["model"],
        resolved_requested_model=target["model"],
        actual_model=target["model"],
        session_id=session_id,
        text=json.dumps(payload, sort_keys=True),
        structured_output=None,
        usage=None,
    )


def _config(
    limits: dict[str, int],
    *,
    participant_count: int = 3,
    max_dissenters: int = 1,
) -> dict[str, Any]:
    return {
        "version": 1,
        "council": {
            "participants": [
                {"id": f"member-{index + 1}", **_target(index)}
                for index in range(participant_count)
            ],
            "moderator": {"runtime": "codex", "model": "moderator-model"},
            "consensus": {"max_dissenters": max_dissenters},
        },
        "limits": copy.deepcopy(limits),
    }


def _participant_target(data: dict[str, Any], index: int) -> AgentTarget:
    participant = DialecticConfig.model_validate(data).council.participants[index]
    return AgentTarget(**participant.model_dump(exclude={"id"}))


def _moderator_target(data: dict[str, Any]) -> AgentTarget:
    moderator = DialecticConfig.model_validate(data).council.moderator
    return AgentTarget(**moderator.model_dump())


async def _scenario(
    tmp_path: Path,
    limits: dict[str, int],
    *,
    participant_count: int = 3,
    max_dissenters: int = 1,
    ballot_kinds: list[str] | None = None,
    ballot_payloads: list[dict[str, Any]] | None = None,
    openings: list[dict[str, Any]] | None = None,
    revisions: list[dict[str, Any]] | None = None,
    candidate: dict[str, Any] | None = None,
    errors: dict[tuple[int, str], BaseException] | None = None,
    delays: dict[tuple[int, str], float] | None = None,
    moderator_error: BaseException | None = None,
    moderator_delay: float = 0.0,
    persistent_index: int | None = 2,
    persistent_close_error: BaseException | None = None,
    config_mutator: Any | None = None,
    opening_callback: Any | None = None,
    adapter_mutator: Any | None = None,
) -> tuple[Any, Any, list[ScriptedAgentAdapter], ScriptedAgentAdapter, RunStore]:
    data = _config(
        limits,
        participant_count=participant_count,
        max_dissenters=max_dissenters,
    )
    if config_mutator is not None:
        config_mutator(data)
    candidate_payload = candidate or _candidate()
    proposition_ids = [item["id"] for item in candidate_payload.get("propositions", [])]
    ballots = ballot_kinds or ["accept"] * participant_count
    openings = openings or [_opening(index) for index in range(participant_count)]
    revisions = revisions or [_revision(index) for index in range(participant_count)]
    errors = errors or {}
    delays = delays or {}
    adapters: list[ScriptedAgentAdapter] = []
    for index in range(participant_count):
        target_data = _target(index)
        session = f"session-{index + 1}"
        phase_payloads = {
            "opening": openings[index],
            "cross-examination": revisions[index],
            "ballot": (
                ballot_payloads[index]
                if ballot_payloads is not None
                else _ballot(ballots[index], proposition_ids)
            ),
        }
        steps = []
        for phase in ("opening", "cross-examination", "ballot"):
            error = errors.get((index, phase))
            steps.append(
                ScriptedStep(
                    response=(
                        None
                        if error is not None
                        else _response(target_data, session, phase_payloads[phase])
                    ),
                    error=error,
                    delay_seconds=delays.get((index, phase), 0.0),
                    callback=opening_callback if phase == "opening" else None,
                )
            )
        adapters.append(
            ScriptedAgentAdapter(
                _participant_target(data, index),
                steps,
                persistent_session=index == persistent_index,
                close_error=(persistent_close_error if index == persistent_index else None),
                stdout_limit=data["limits"]["max_agent_stdout_bytes"],
                stderr_limit=data["limits"]["max_agent_stderr_bytes"],
            )
        )
    if adapter_mutator is not None:
        adapter_mutator(adapters)
    moderator_target = _moderator_target(data)
    moderator = ScriptedAgentAdapter(
        moderator_target,
        [
            ScriptedStep(
                response=(
                    None
                    if moderator_error is not None
                    else _response(
                        {"runtime": moderator_target.runtime, "model": moderator_target.model},
                        "moderator-fresh-session",
                        candidate_payload,
                    )
                ),
                error=moderator_error,
                delay_seconds=moderator_delay,
            )
        ],
    )
    store = RunStore(
        tmp_path / "state",
        run_id_factory=lambda: "20260829T000000Z-aaaaaaaaaa",
    )
    orchestrator = CouncilOnceOrchestrator(
        participant_adapters={f"member-{index + 1}": adapter for index, adapter in enumerate(adapters)},
        moderator_adapter=moderator,
    )
    service = DialecticService(store, council_executor=orchestrator)
    handle = service.create_run("council")
    record = await service.execute_council_once(
        handle,
        config_bytes=yaml.safe_dump(data, sort_keys=False).encode("utf-8"),
        prompt_bytes=b"Choose the safest bounded option.",
    )
    return record, handle, adapters, moderator, store


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_council_001_complete_three_participant_artifact_and_persistent_lifecycle(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    callback_paths: list[Path] = []

    def barrier_callback(_request: Any) -> None:
        callback_paths.extend((tmp_path / "state" / "runs").glob("*/audit/capabilities/participant/*/opening.binding.json"))
        assert len(callback_paths) >= 3

    record, handle, adapters, moderator, _store = await _scenario(
        tmp_path, limits, opening_callback=barrier_callback
    )
    assert (record.status, record.consensus_outcome) == ("FINALIZED", "UNANIMOUS")
    assert [len(adapter.invocations) for adapter in adapters] == [3, 3, 3]
    assert len(moderator.invocations) == 1
    assert [item.operation for item in adapters[2].invocations] == ["start", "resume", "resume"]
    attempts = sorted(handle.path.glob("turns/*/*/*.attempt.json"))
    assert len(attempts) == 10
    assert not list(handle.path.rglob("*.response.json"))
    for path in attempts:
        artifact = TurnAttemptArtifact.model_validate_json(path.read_bytes(), strict=True)
        root = path.with_suffix("").with_suffix("")
        for suffix, stream in ((".stdout.txt", artifact.stdout), (".stderr.txt", artifact.stderr)):
            persisted = Path(str(root) + suffix).read_bytes()
            assert len(persisted) == stream.persisted_bytes
            assert hashlib.sha256(persisted).hexdigest() == stream.persisted_sha256
        request = Path(str(root) + ".request.json").read_bytes()
        assert hashlib.sha256(request).hexdigest() == artifact.request_artifact_sha256
    grok = [
        TurnAttemptArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in sorted(handle.path.glob("turns/participant/participant-c/*.attempt.json"))
    ]
    by_phase = {item.turn_phase: item for item in grok}
    assert [by_phase[name].process_origin for name in ("opening", "cross-examination", "ballot")] == [
        "spawned-for-attempt", "retained-from-prior-turn", "retained-from-prior-turn"
    ]
    assert [by_phase[name].process_disposition for name in ("opening", "cross-examination", "ballot")] == [
        "retained-for-session", "retained-for-session", "closed"
    ]
    assert [by_phase[name].process_exit_code for name in ("opening", "cross-examination", "ballot")] == [
        None, None, 0
    ]
    assert all(item.attempt_end_reason == "response-returned" for item in grok)
    assert len({item.process_unit_id for item in grok}) == 1
    assert adapters[2].close_count == 1
    grok_preflight = _read_json(
        handle.path / "audit/targets/participant/participant-c.json"
    )
    assert (grok_preflight["prompt_transport"], grok_preflight["process_lifecycle"]) == (
        "acp-stdio",
        "persistent-acp-session",
    )
    for path in handle.path.glob("audit/capabilities/participant/*/*.binding.json"):
        binding = CapabilityBindingArtifact.model_validate_json(path.read_bytes(), strict=True)
        assert [identity.role for identity in binding.dynamic_filesystem_identities] == [
            "neutral_role_dir"
        ]
    events = [json.loads(line) for line in (handle.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    persistent_events = [
        event for event in events if event["event_type"] in {
            "session_lease_acquired", "capture_epoch_closed", "session_lease_closed"
        }
    ]
    assert [event["event_type"] for event in persistent_events] == [
        "session_lease_acquired",
        "capture_epoch_closed",
        "capture_epoch_closed",
        "capture_epoch_closed",
        "session_lease_closed",
    ]
    assert {event["payload"]["process_unit_id"] for event in persistent_events} == {
        by_phase["opening"].process_unit_id
    }

    identity_root = tmp_path / "identity-change"
    neutral = (
        identity_root
        / "state"
        / "runs"
        / "20260829T000000Z-aaaaaaaaaa"
        / "council-role-directories"
        / "participant-c"
    )

    def replace_bound_directory(adapters: list[ScriptedAgentAdapter]) -> None:
        original_prepare = adapters[2].prepare_resume
        replaced = False

        async def prepare_with_replacement(session_id: str, request: Any) -> object:
            nonlocal replaced
            evidence = await original_prepare(session_id, request)
            if not replaced:
                neutral.rename(neutral.with_name(f"{neutral.name}-displaced"))
                neutral.mkdir(mode=0o700)
                replaced = True
            return evidence

        adapters[2].prepare_resume = prepare_with_replacement  # type: ignore[method-assign]

    changed, changed_handle, changed_adapters, changed_moderator, _ = await _scenario(
        identity_root,
        limits,
        adapter_mutator=replace_bound_directory,
    )
    assert (changed.status, changed.failure_kind) == ("FAILED", "PREFLIGHT_FAILED")
    assert [turn.turn_phase for turn in changed_adapters[2].invocations] == ["opening"]
    assert all(
        turn.turn_phase in {"opening", "cross-examination"}
        for adapter in changed_adapters[:2]
        for turn in adapter.invocations
    )
    assert changed_adapters[2].close_count == 1
    assert not changed_moderator.invocations
    assert not (
        changed_handle.path / "council/cross-examination/participant-c.json"
    ).exists()

    def reject_unqualified_cli(adapters: list[ScriptedAgentAdapter]) -> None:
        async def fail_preflight(_target: AgentTarget) -> Any:
            raise NativePreflightError(
                "Codex CLI 0.152.0 is installed but has not been qualified by Dialectic 0.1.0"
            )

        adapters[0].preflight = fail_preflight  # type: ignore[method-assign]

    failed, *_ = await _scenario(
        tmp_path / "preflight-diagnostic",
        limits,
        adapter_mutator=reject_unqualified_cli,
    )
    assert (failed.status, failed.failure_kind) == ("FAILED", "PREFLIGHT_FAILED")
    assert failed.failure_detail == (
        "target preflight failed for member-1: Codex CLI 0.152.0 is installed but "
        "has not been qualified by Dialectic 0.1.0"
    )


@pytest.mark.asyncio
async def test_council_002_openings_are_blind_and_identical(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, _handle, adapters, _moderator, _store = await _scenario(tmp_path, limits)
    prompts = [adapter.invocations[0].prompt for adapter in adapters]
    assert len(set(prompts)) == 1
    assert "Participant A" not in prompts[0]
    assert "Independent conclusion" not in prompts[0]


@pytest.mark.asyncio
async def test_council_003_cross_examination_uses_aliases_not_provider_brands(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, _handle, adapters, _moderator, _store = await _scenario(tmp_path, limits)
    packet = adapters[0].invocations[1].prompt
    assert all(alias in packet for alias in ("Participant A", "Participant B", "Participant C"))
    assert all(brand not in packet for brand in ("codex-model", "claude-model", "grok-model"))


@pytest.mark.asyncio
async def test_council_004_changed_mind_and_reason_are_retained(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, handle, _adapters, _moderator, _store = await _scenario(tmp_path, limits)
    revision = _read_json(handle.path / "council/cross-examination/participant-b.json")["revision"]
    assert revision["changed_mind"] is True
    assert revision["change_reason"] == "The opposing evidence was stronger"


@pytest.mark.asyncio
async def test_council_005_moderator_is_fresh_and_non_voting(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, handle, adapters, moderator, _store = await _scenario(tmp_path, limits)
    assert [(item.operation, item.session_id) for item in moderator.invocations] == [("start", None)]
    assert moderator not in adapters
    assert not (handle.path / "council/ballots/moderator.json").exists()


@pytest.mark.asyncio
async def test_council_006_every_ballot_covers_every_proposition_once(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, handle, _adapters, _moderator, _store = await _scenario(tmp_path, limits)
    for path in handle.path.glob("council/ballots/*.json"):
        ballot = DerivedBallot.model_validate_json(path.read_bytes(), strict=True)
        assert [vote.proposition_id for vote in ballot.ballot.proposition_votes] == ["p-1", "p-2"]


@pytest.mark.asyncio
async def test_council_007_three_accepts_are_unanimous(tmp_path: Path, limits: dict[str, int]) -> None:
    record, *_ = await _scenario(tmp_path, limits, ballot_kinds=["accept"] * 3)
    assert (record.status, record.consensus_outcome) == ("FINALIZED", "UNANIMOUS")


@pytest.mark.asyncio
async def test_council_008_two_accepts_one_reject_is_rough_consensus_with_minority(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    record, handle, *_ = await _scenario(
        tmp_path, limits, ballot_kinds=["accept", "accept", "reject"]
    )
    assert record.consensus_outcome == "ROUGH_CONSENSUS"
    assert "I reject the candidate" in (handle.path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_council_009_blocker_forces_contested(tmp_path: Path, limits: dict[str, int]) -> None:
    record, *_ = await _scenario(tmp_path, limits, ballot_kinds=["accept", "accept", "blocker"])
    assert record.consensus_outcome == "CONTESTED"


@pytest.mark.asyncio
async def test_council_010_zero_dissenters_and_one_abstention_is_contested(tmp_path: Path, limits: dict[str, int]) -> None:
    record, *_ = await _scenario(
        tmp_path, limits, max_dissenters=0, ballot_kinds=["accept", "accept", "abstain"]
    )
    assert record.consensus_outcome == "CONTESTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["opening", "cross-examination", "ballot"])
async def test_council_011_participant_failure_reaps_active_and_retained_peers(
    tmp_path: Path, limits: dict[str, int], phase: str
) -> None:
    record, handle, adapters, _moderator, _store = await _scenario(
        tmp_path,
        limits,
        errors={(0, phase): RuntimeError("participant failed")},
        delays={(1, phase): 1.0},
    )
    assert (record.status, record.failure_kind) == ("FAILED", "NO_QUORUM")
    assert adapters[2].close_count == 1
    attempts = [
        TurnAttemptArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in handle.path.glob("turns/participant/*/*.attempt.json")
    ]
    grok_attempts = [
        attempt for attempt in attempts if attempt.target_id == "participant-c"
    ]
    per_turn_attempts = [
        attempt for attempt in attempts if attempt.target_id in {"participant-a", "participant-b"}
    ]
    assert per_turn_attempts
    assert all(attempt.process_disposition in {"closed", "not-started"} for attempt in per_turn_attempts)
    assert all(attempt.process_disposition != "cleanup-failed" for attempt in per_turn_attempts)
    assert grok_attempts
    phase_order = {"opening": 0, "cross-examination": 1, "ballot": 2}
    latest_grok = max(grok_attempts, key=lambda item: phase_order[item.turn_phase])
    assert latest_grok.turn_phase == phase
    assert latest_grok.process_disposition == "closed"
    if phase == "ballot":
        assert latest_grok.attempt_end_reason == "response-returned"
    else:
        assert latest_grok.attempt_end_reason in {"peer-failure", "agent-failed"}

    allowed_phases = tuple(phase_order)[: phase_order[phase] + 1]
    assert all(
        invocation.turn_phase in allowed_phases
        for adapter in adapters
        for invocation in adapter.invocations
    )
    events = [
        json.loads(line)
        for line in (handle.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    event_types = [event["event_type"] for event in events]
    assert event_types.count("capture_epoch_closed") == phase_order[phase] + 1
    assert event_types.count("session_lease_closed") == 1
    assert event_types.index("session_lease_closed") < event_types.index("run-failed")
    assert event_types[-1] == "run-failed"
    assert not list(handle.path.glob("turns/participant/*/*.response.json"))
    assert record.phase == {
        "opening": "OPENING_POSITIONS",
        "cross-examination": "CROSS_EXAMINATION",
        "ballot": "BALLOTS",
    }[phase]
    later = {"opening": "cross-examination", "cross-examination": "candidate", "ballot": "report"}[phase]
    if later == "cross-examination":
        assert not list(handle.path.glob("council/cross-examination/*.json"))
    elif later == "candidate":
        assert not (handle.path / "council/candidate.json").exists()


@pytest.mark.asyncio
async def test_council_012_moderator_failure_closes_retained_sessions(tmp_path: Path, limits: dict[str, int]) -> None:
    record, handle, adapters, _moderator, _store = await _scenario(
        tmp_path, limits, moderator_error=RuntimeError("moderator failed")
    )
    assert (record.status, record.failure_kind) == ("FAILED", "MODERATOR_FAILED")
    assert adapters[2].close_count == 1
    assert not list(handle.path.glob("council/ballots/*.json"))
    grok_cross = TurnAttemptArtifact.model_validate_json(
        (handle.path / "turns/participant/participant-c/cross-examination.attempt.json").read_bytes(),
        strict=True,
    )
    assert (grok_cross.process_disposition, grok_cross.attempt_end_reason) == (
        "closed",
        "peer-failure",
    )
    events = [
        json.loads(line)
        for line in (handle.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    event_types = [event["event_type"] for event in events]
    assert event_types.count("capture_epoch_closed") == 2
    assert event_types.count("session_lease_closed") == 1
    assert event_types.index("session_lease_closed") < event_types.index("run-failed")

    cleanup_record, cleanup_handle, cleanup_adapters, *_ = await _scenario(
        tmp_path / "cleanup",
        limits,
        moderator_error=RuntimeError("moderator failed"),
        persistent_close_error=NativeTurnError(
            "PROCESS_CLEANUP_FAILED", "scripted cleanup failure"
        ),
    )
    assert (cleanup_record.status, cleanup_record.failure_kind) == (
        "FAILED",
        "PROCESS_CLEANUP_FAILED",
    )
    assert cleanup_adapters[2].close_count == 1
    cleanup_cross = TurnAttemptArtifact.model_validate_json(
        (
            cleanup_handle.path
            / "turns/participant/participant-c/cross-examination.attempt.json"
        ).read_bytes(),
        strict=True,
    )
    assert (cleanup_cross.process_disposition, cleanup_cross.attempt_end_reason) == (
        "cleanup-failed",
        "cleanup-failed",
    )
    assert not list(cleanup_handle.path.glob("council/ballots/*.json"))


@pytest.mark.asyncio
async def test_council_013_overall_timeout_waits_for_retained_cleanup_and_cleanup_overrides(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    short = copy.deepcopy(limits)
    short["council_run_seconds"] = 1
    record, _handle, adapters, _moderator, _store = await _scenario(
        tmp_path / "normal", short, moderator_delay=2.0
    )
    assert record.status == "TIMED_OUT"
    assert adapters[2].close_count == 1
    record2, *_ = await _scenario(
        tmp_path / "cleanup",
        short,
        moderator_delay=2.0,
        persistent_close_error=NativeTurnError(
            "PROCESS_CLEANUP_FAILED", "scripted cleanup failure"
        ),
    )
    assert (record2.status, record2.failure_kind) == ("FAILED", "PROCESS_CLEANUP_FAILED")


@pytest.mark.asyncio
async def test_council_014_exact_round_count_and_no_retained_lease_survives(tmp_path: Path, limits: dict[str, int]) -> None:
    record, _handle, adapters, _moderator, _store = await _scenario(tmp_path, limits)
    assert record.status == "FINALIZED"
    assert [[item.turn_phase for item in adapter.invocations] for adapter in adapters] == [
        ["opening", "cross-examination", "ballot"]
    ] * 3
    assert adapters[2].close_count == 1


@pytest.mark.asyncio
async def test_council_015_report_contains_required_user_facing_sections(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, handle, *_ = await _scenario(
        tmp_path, limits, ballot_kinds=["accept", "reject", "blocker"]
    )
    report = (handle.path / "summary.md").read_text(encoding="utf-8")
    for value in (
        "Council answer", "Vote matrix", "Rationale", "minority report",
        "blocking objection", "Unresolved questions", "Participant identities",
        "codex-model", "claude-model", "grok-model",
    ):
        assert value in report


def test_council_016_negative_max_dissenters_is_rejected(limits: dict[str, int]) -> None:
    data = _config(limits)
    data["council"]["consensus"]["max_dissenters"] = -1
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        DialecticConfig.model_validate(data)


def test_council_017_max_dissenters_at_least_participant_count_names_both_values(limits: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match=r"max_dissenters \(3\).*participant count \(3\)"):
        DialecticConfig.model_validate(_config(limits, max_dissenters=3))


@pytest.mark.asyncio
async def test_council_018_every_reject_is_contested(tmp_path: Path, limits: dict[str, int]) -> None:
    record, *_ = await _scenario(tmp_path, limits, ballot_kinds=["reject"] * 3)
    assert record.consensus_outcome == "CONTESTED"


@pytest.mark.asyncio
async def test_council_019_unanimous_precedes_rough_consensus(tmp_path: Path, limits: dict[str, int]) -> None:
    record, *_ = await _scenario(tmp_path, limits, ballot_kinds=["accept"] * 3)
    assert record.consensus_outcome == "UNANIMOUS"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(0),
        _candidate(2, propositions=[_candidate(1)["propositions"][0]] * 2),
        _candidate(1, propositions=[{**_candidate(1)["propositions"][0], "id": ""}]),
        _candidate(1, propositions=[{**_candidate(1)["propositions"][0], "supporting_participants": ["Unknown"]}]),
    ],
)
async def test_council_020_invalid_candidate_shapes_fail_moderation(
    tmp_path: Path, limits: dict[str, int], candidate: dict[str, Any]
) -> None:
    record, *_ = await _scenario(tmp_path, limits, candidate=candidate)
    assert (record.status, record.failure_kind) == ("FAILED", "MODERATOR_FAILED")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["duplicate", "omit", "invent"])
async def test_council_021_invalid_proposition_vote_sets_fail_quorum(
    tmp_path: Path, limits: dict[str, int], shape: str
) -> None:
    candidate = _candidate()
    ballot = _ballot("accept", ["p-1", "p-2"])
    if shape == "duplicate":
        ballot["proposition_votes"][1]["proposition_id"] = "p-1"
    elif shape == "omit":
        ballot["proposition_votes"].pop()
    else:
        ballot["proposition_votes"][1]["proposition_id"] = "p-3"
    invalid_record, *_ = await _scenario(
        tmp_path,
        limits,
        candidate=candidate,
        ballot_payloads=[
            ballot,
            _ballot("accept", ["p-1", "p-2"]),
            _ballot("accept", ["p-1", "p-2"]),
        ],
    )
    assert (invalid_record.status, invalid_record.failure_kind) == ("FAILED", "NO_QUORUM")


def test_council_022_blocking_flag_and_evidence_must_be_consistent() -> None:
    with pytest.raises(ValidationError, match="requires non-empty evidence"):
        CouncilBallot.model_validate({**_ballot("blocker", ["p-1"]), "blocking_objection_evidence": ""})
    with pytest.raises(ValidationError, match="requires null blocking evidence"):
        CouncilBallot.model_validate({**_ballot("accept", ["p-1"]), "blocking_objection_evidence": "surplus"})


@pytest.mark.asyncio
async def test_council_023_controller_derives_all_four_ballot_combinations(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, handle, *_ = await _scenario(
        tmp_path,
        limits,
        participant_count=4,
        max_dissenters=1,
        ballot_kinds=["accept", "abstain", "reject", "blocker"],
        persistent_index=2,
    )
    values = [
        _read_json(path)["derived_overall_vote"]
        for path in sorted(handle.path.glob("council/ballots/*.json"))
    ]
    assert values == ["accept", "abstain", "reject", "reject"]
    assert all("overall_vote" not in _read_json(path)["ballot"] for path in handle.path.glob("council/ballots/*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["session", "lease"])
async def test_council_024_missing_session_or_persistent_lease_stops_before_cross_exam(
    tmp_path: Path, limits: dict[str, int], missing: str
) -> None:
    if missing == "session":
        opening_values = [_opening(i) for i in range(3)]
        data = _config(limits)
        adapters = []
        for index in range(3):
            target_data = _target(index)
            session = None if index == 0 else f"session-{index + 1}"
            adapters.append(
                ScriptedAgentAdapter(
                    _participant_target(data, index),
                    [ScriptedStep(response=_response(target_data, session, opening_values[index]))],
                    persistent_session=index == 2,
                )
            )
        moderator_target = _moderator_target(data)
        moderator = ScriptedAgentAdapter(moderator_target, [])
        store = RunStore(tmp_path / "state", run_id_factory=lambda: "20260829T000002Z-aaaaaaaaaa")
        service = DialecticService(
            store,
            council_executor=CouncilOnceOrchestrator(
                participant_adapters={f"member-{i + 1}": adapter for i, adapter in enumerate(adapters)},
                moderator_adapter=moderator,
            ),
        )
        handle = service.create_run("council")
        record = await service.execute_council_once(
            handle, config_bytes=yaml.safe_dump(data).encode(), prompt_bytes=b"prompt"
        )
    else:
        def remove_lease_evidence(adapters: list[ScriptedAgentAdapter]) -> None:
            adapters[2].prepare_resume = None  # type: ignore[method-assign]

        record, handle, adapters, moderator, _store = await _scenario(
            tmp_path, limits, adapter_mutator=remove_lease_evidence
        )
    assert (record.status, record.failure_kind) == ("FAILED", "NO_QUORUM")
    assert not list(handle.path.glob("council/cross-examination/*.json"))
    assert not moderator.invocations


@pytest.mark.asyncio
async def test_council_025_participant_b_receives_complete_self_identified_ledger(tmp_path: Path, limits: dict[str, int]) -> None:
    _record, _handle, adapters, *_ = await _scenario(tmp_path, limits)
    packet = json.loads(adapters[1].invocations[1].prompt)
    assert packet["self_alias"] == "Participant B"
    assert [entry["alias"] for entry in packet["position_ledger"]["positions"]] == [
        "Participant A", "Participant B", "Participant C"
    ]
    assert "runtime" not in packet["position_ledger"]
    assert "model" not in packet["position_ledger"]


@pytest.mark.parametrize("mutation", ["one", "six", "duplicate", "invalid"])
def test_council_026_participant_count_and_id_contract(mutation: str, limits: dict[str, int]) -> None:
    count = 2 if mutation in {"duplicate", "invalid"} else (1 if mutation == "one" else 5)
    data = _config(limits, participant_count=count)
    if mutation == "six":
        data["council"]["participants"].append({"id": "member-6", **_target(0)})
    elif mutation == "duplicate":
        data["council"]["participants"][1]["id"] = "member-1"
    elif mutation == "invalid":
        data["council"]["participants"][1]["id"] = "Bad ID"
    with pytest.raises(ValidationError):
        DialecticConfig.model_validate(data)


@pytest.mark.asyncio
async def test_council_027_wall_clock_reaps_active_and_retained_units_before_timeout(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    short = copy.deepcopy(limits)
    short["council_run_seconds"] = 1
    record, handle, adapters, _moderator, _store = await _scenario(
        tmp_path,
        short,
        delays={(0, "opening"): 2.0, (1, "opening"): 2.0},
    )
    assert record.status == "TIMED_OUT"
    assert adapters[2].close_count == 1
    terminal = _read_json(handle.path / "run.json")
    assert terminal["status"] == "TIMED_OUT"
    attempts = [
        TurnAttemptArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in handle.path.glob("turns/participant/*/*.attempt.json")
    ]
    assert attempts and all(item.process_disposition != "retained-for-session" for item in attempts)
    assert any(item.attempt_end_reason == "timeout" for item in attempts)
    assert all(item.attempt_end_reason not in {"cancelled", "peer-failure"} for item in attempts)


@pytest.mark.asyncio
async def test_council_028_out_of_range_opening_confidence_is_no_quorum(tmp_path: Path, limits: dict[str, int]) -> None:
    openings = [_opening(0, confidence=1.01), _opening(1), _opening(2)]
    record, *_ = await _scenario(tmp_path, limits, openings=openings)
    assert (record.status, record.failure_kind) == ("FAILED", "NO_QUORUM")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["cross-examination", "ballot"])
async def test_council_029_oversized_phase_packet_starts_no_participant(
    tmp_path: Path, limits: dict[str, int], phase: str
) -> None:
    bounded = copy.deepcopy(limits)
    opening_packet = {
        "prompt": _opening_prompt("Choose the safest bounded option."),
        "output_schema": OpeningPosition.model_json_schema(),
    }
    opening_size = len(canonical_mapping_bytes(opening_packet))
    bounded["max_packet_bytes"] = opening_size + 800
    if phase == "cross-examination":
        openings = [_opening(i, conclusion="x" * 1000) for i in range(3)]
        record, _handle, adapters, moderator, _store = await _scenario(
            tmp_path, bounded, openings=openings
        )
        assert all(len(adapter.invocations) == 1 for adapter in adapters)
        assert not moderator.invocations
    else:
        candidate = _candidate(answer="x" * 3000)
        bounded["max_packet_bytes"] = 5_000
        record, _handle, adapters, moderator, _store = await _scenario(
            tmp_path, bounded, candidate=candidate
        )
        assert all(len(adapter.invocations) == 2 for adapter in adapters)
        assert len(moderator.invocations) == 1
    assert (record.status, record.failure_kind) == ("FAILED", "PACKET_TOO_LARGE")


@pytest.mark.asyncio
@pytest.mark.parametrize("count,passes", [(0, False), (2, True), (3, False)])
async def test_council_030_candidate_proposition_controller_bound(
    tmp_path: Path, limits: dict[str, int], count: int, passes: bool
) -> None:
    bounded = copy.deepcopy(limits)
    bounded["max_propositions"] = 2
    record, *_ = await _scenario(tmp_path, bounded, candidate=_candidate(count))
    assert (record.status == "FINALIZED") is passes
    if not passes:
        assert record.failure_kind == "MODERATOR_FAILED"


@pytest.mark.asyncio
async def test_council_031_authored_self_identification_is_preserved_not_rewritten(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    marker = "I am running Claude Code and this prose identifies me."
    openings = [_opening(0), _opening(1, conclusion=marker), _opening(2)]
    _record, _handle, adapters, moderator, _store = await _scenario(
        tmp_path, limits, openings=openings
    )
    assert marker in adapters[0].invocations[1].prompt
    assert marker in moderator.invocations[0].prompt


@pytest.mark.asyncio
async def test_council_032_two_accepts_one_abstention_is_rough_consensus_and_freeze_is_108(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    record, *_ = await _scenario(
        tmp_path, limits, ballot_kinds=["accept", "accept", "abstain"]
    )
    assert record.consensus_outcome == "ROUGH_CONSENSUS"
    root = Path(__file__).parent
    inventories = {
        "CORE": (root / "test_core.py", 30),
        "CODE": (root / "test_code_once.py", 46),
        "COUNCIL": (root / "test_council_once.py", 32),
    }
    total = 0
    for prefix, (path, count) in inventories.items():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        pattern = re.compile(rf"^test_{prefix.lower()}_(\d{{3}})(?:_|$)")
        matches = [
            f"{prefix}-{match.group(1)}"
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if (match := pattern.match(node.name)) is not None
        ]
        found = set(matches)
        assert len(matches) == count
        assert found == {f"{prefix}-{index:03d}" for index in range(1, count + 1)}
        total += len(found)
    assert total == 108

    for path, _ in inventories.values():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        response_suffix = ".response" + ".json"
        response_literals = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and response_suffix in node.value
        ]
        for literal in response_literals:
            ancestor: ast.AST | None = literal
            while ancestor is not None and not isinstance(ancestor, ast.Assert):
                ancestor = parents.get(ancestor)
            assert isinstance(ancestor, ast.Assert)
            assert isinstance(ancestor.test, ast.UnaryOp)
            assert isinstance(ancestor.test.op, ast.Not)

    assert {"process_started", "termination_reason"}.isdisjoint(
        TurnAttemptArtifact.model_fields
    )
    assert get_type_hints(AgentAdapter)["process_local_continuation"] is bool
    assert get_args(get_type_hints(AgentAdapter)["prompt_transport"]) == (
        "stdin",
        "acp-stdio",
    )
    assert DynamicFilesystemIdentity.model_fields["filesystem_identity"].is_required()
    assert set(get_args(DynamicFilesystemIdentity.model_fields["role"].annotation)) == {
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
    }
    evidence_path = root.parent / "src" / "dialectic" / "workflow_evidence.py"
    evidence_source = evidence_path.read_text(encoding="utf-8")
    evidence_tree = ast.parse(evidence_source, filename=str(evidence_path))
    persist_gate_a = next(
        node
        for node in ast.walk(evidence_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "persist_gate_a"
    )
    persistence_source = ast.get_source_segment(evidence_source, persist_gate_a)
    assert persistence_source is not None
    assert "adapter.process_local_continuation" in persistence_source
    assert "grok-build" not in persistence_source
