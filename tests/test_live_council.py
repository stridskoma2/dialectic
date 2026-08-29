"""Opt-in, cost-bearing native Council Once release smoke."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from dialectic.native_adapters import recorded_probe_provider
from dialectic.native_runtime import NativeCouncilExecutor, native_credentials
from dialectic.schemas import (
    CandidateConclusionArtifact,
    CouncilRevisionArtifact,
    DerivedBallot,
    LimitsSpec,
    OpeningPositionArtifact,
    SummaryRecord,
    TurnAttemptArtifact,
)
from dialectic.service import DialecticService
from dialectic.store import RunStore

pytestmark = pytest.mark.live


def _available_targets() -> list[dict[str, str]]:
    if os.environ.get("DIALECTIC_LIVE") != "1":
        pytest.skip("set DIALECTIC_LIVE=1 for authenticated, cost-bearing native tests")
    candidates = (
        ("codex", "codex", "DIALECTIC_CODEX_MODEL"),
        ("claude", "claude-code", "DIALECTIC_CLAUDE_MODEL"),
        ("grok", "grok-build", "DIALECTIC_GROK_MODEL"),
    )
    targets: list[dict[str, str]] = []
    for identifier, runtime, variable in candidates:
        model = os.environ.get(variable)
        if model and shutil.which(identifier):
            target = {"id": identifier, "runtime": runtime, "model": model}
            if runtime == "codex":
                target["effort"] = "low"
            targets.append(target)
    if len(targets) < 2:
        pytest.fail(
            "the live council smoke requires model variables and installed pinned CLIs "
            "for at least two of Codex, Claude Code, and Grok Build"
        )
    return targets[:3]


@pytest.mark.asyncio
async def test_live_council_smoke_produces_all_structured_stages(
    tmp_path: Path,
) -> None:
    participants = _available_targets()
    moderator_source = next(
        (target for target in participants if target["runtime"] == "codex"),
        participants[0],
    )
    moderator = {
        key: value for key, value in moderator_source.items() if key != "id"
    }
    limits = _live_limits().model_copy(
        update={"max_council_participants": len(participants)}
    )
    config = {
        "version": 1,
        "council": {
            "participants": participants,
            "moderator": moderator,
            "consensus": {"max_dissenters": 1},
        },
        "limits": limits.model_dump(),
    }
    store = RunStore(tmp_path / "state")
    service = DialecticService(
        store,
        credential_provider=lambda loaded, mode: native_credentials(
            loaded, mode, environment=os.environ
        ),
        council_executor=NativeCouncilExecutor(
            source_environment=os.environ,
            probe_provider=recorded_probe_provider,
        ),
    )
    handle = service.create_run("council")
    record = await service.execute_council_once(
        handle,
        config_bytes=yaml.safe_dump(config, sort_keys=False).encode("utf-8"),
        prompt_bytes=(
            b"For a small local audit trail, compare an append-only JSONL log with "
            b"a relational database. Recommend a bounded default and identify when "
            b"the alternative becomes preferable. This is an architecture question only."
        ),
    )

    assert record.status == "FINALIZED"
    assert record.consensus_outcome in {
        "UNANIMOUS",
        "ROUGH_CONSENSUS",
        "CONTESTED",
    }
    openings = [
        OpeningPositionArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in sorted((handle.path / "council" / "opening").glob("*.json"))
    ]
    revisions = [
        CouncilRevisionArtifact.model_validate_json(path.read_bytes(), strict=True)
        for path in sorted(
            (handle.path / "council" / "cross-examination").glob("*.json")
        )
    ]
    ballots = [
        DerivedBallot.model_validate_json(path.read_bytes(), strict=True)
        for path in sorted((handle.path / "council" / "ballots").glob("*.json"))
    ]
    candidate = CandidateConclusionArtifact.model_validate_json(
        (handle.path / "council" / "candidate.json").read_bytes(), strict=True
    )
    summary = SummaryRecord.model_validate_json(
        (handle.path / "summary.json").read_bytes(), strict=True
    )
    report = (handle.path / "summary.md").read_text(encoding="utf-8")
    assert len(openings) == len(revisions) == len(ballots) == len(participants)
    assert candidate.candidate.propositions
    assert summary.outcome == record.consensus_outcome
    assert f"Outcome: {record.consensus_outcome}" in report
    assert all(section in report for section in ("Council answer", "Vote matrix", "Dissent"))

    grok_index = next(
        (index for index, target in enumerate(participants) if target["runtime"] == "grok-build"),
        None,
    )
    if grok_index is not None:
        target_id = f"participant-{chr(ord('a') + grok_index)}"
        attempts = [
            TurnAttemptArtifact.model_validate_json(
                (
                    handle.path
                    / "turns"
                    / "participant"
                    / target_id
                    / f"{phase}.attempt.json"
                ).read_bytes(),
                strict=True,
            )
            for phase in ("opening", "cross-examination", "ballot")
        ]
        assert len(attempts) == 3
        assert len({attempt.process_unit_id for attempt in attempts}) == 1
        assert [attempt.process_origin for attempt in attempts] == [
            "spawned",
            "retained",
            "retained",
        ]
        assert attempts[-1].process_disposition == "closed"


def _live_limits() -> LimitsSpec:
    return LimitsSpec(
        max_reviewers=1,
        max_findings_per_reviewer=1,
        max_total_findings=1,
        max_council_participants=3,
        max_propositions=5,
        max_config_bytes=262_144,
        max_input_bytes=262_144,
        max_diff_bytes=1_048_576,
        max_changed_paths=1_000,
        max_changed_regular_file_bytes=67_108_864,
        max_candidate_change_bytes=268_435_456,
        max_packet_bytes=1_572_864,
        max_lens_chars=8_192,
        max_model_field_chars=65_536,
        max_model_list_items=500,
        max_agent_stdout_bytes=1_048_576,
        max_agent_stderr_bytes=262_144,
        max_turn_scratch_bytes=1_048_576,
        max_turn_scratch_entries=1_000,
        max_turn_scratch_depth=16,
        preflight_seconds=120,
        capability_probe_seconds=300,
        agent_turn_seconds=180,
        code_run_seconds=600,
        council_run_seconds=900,
        graceful_kill_seconds=10,
        turn_cleanup_seconds=30,
        code_review_cycles=1,
        council_discussion_rounds=1,
    )
