from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import pytest
import yaml
from pydantic import ValidationError

from dialectic.adapters import AgentProcessError, ScriptedAgentAdapter, ScriptedStep
from dialectic.capabilities import (
    CapabilityEvidenceError,
    CapabilityFixture,
    build_capability_binding,
    validate_binding_identities,
)
from dialectic.code_once import CodeOnceOrchestrator
from dialectic.codex_policy import (
    CodexConstructionFixture,
    CodexPolicyError,
    build_codex_driver_construction,
)
from dialectic.git_workspace import (
    ChangeValidator,
    GitRunner,
    GitWorkflowError,
    GitWorkspace,
)
from dialectic.locking import RepositoryBusyError, RepositoryLock, resolve_repository_identity
from dialectic.schemas import (
    AgentResponse,
    AgentTarget,
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    LimitsSpec,
    RunRecord,
    SummaryRecord,
    TargetPreflightArtifact,
    TurnAttemptArtifact,
    WorkspaceRecord,
)
from dialectic.scratch import ScratchCleanupTimeout, ScratchContainmentError
from dialectic.service import DialecticService
from dialectic.store import RunHandle, RunStore
from dialectic.turn_workspace import TurnWorkspace


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _make_repo(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "app.txt")
    _git(repository, "commit", "-m", "base")
    return repository


def _response(
    target: AgentTarget,
    *,
    text: str = "done",
    session_id: str | None = "session-1",
    actual_model: str | None = None,
) -> AgentResponse:
    return AgentResponse(
        artifact_schema_version=1,
        tool_version="0.1.0",
        runtime=target.runtime,
        requested_model=target.model,
        resolved_requested_model=target.model,
        actual_model=actual_model,
        session_id=session_id,
        text=text,
        structured_output=None,
        usage=None,
    )


def _edit_step(
    target: AgentTarget,
    *,
    name: str = "feature.txt",
    content: bytes = b"implemented\n",
    session_id: str = "driver-session",
) -> ScriptedStep:
    def edit(request) -> None:  # type: ignore[no-untyped-def]
        Path(request.working_directory, name).write_bytes(content)

    return ScriptedStep(response=_response(target, session_id=session_id), callback=edit)


def _review_step(
    target: AgentTarget,
    findings: list[dict[str, object]],
    *,
    summary: str = "reviewed",
    delay: float = 0,
    actual_model: str | None = None,
) -> ScriptedStep:
    step = ScriptedStep(delay_seconds=delay)

    def answer(request) -> None:  # type: ignore[no-untyped-def]
        packet = json.loads(request.prompt)
        core = packet["core"]
        report = {
            "schema_version": 1,
            "base_sha": core["base_sha"],
            "head_sha": core["review_sha"],
            "verdict": "changes_requested" if findings else "pass",
            "summary": summary,
            "findings": findings,
        }
        step.response = _response(
            target,
            text=json.dumps(report),
            session_id=f"review-{target.runtime}",
            actual_model=actual_model,
        )

    step.callback = answer
    return step


def _finding(local_id: str = "F1", *, file: str | None = "feature.txt") -> dict[str, object]:
    return {
        "id": local_id,
        "severity": "major",
        "category": "correctness",
        "file": file,
        "line": 1,
        "claim": "The change needs a guard.",
        "evidence": "The new line is unguarded.",
        "suggested_fix": "Add the guard.",
    }


def _repair_step(
    target: AgentTarget,
    outcomes: list[str] | Callable[[list[str]], list[str]],
    *,
    edit: bool,
) -> ScriptedStep:
    step = ScriptedStep()

    def repair(request) -> None:  # type: ignore[no-untyped-def]
        packet = json.loads(request.prompt)
        keys = [item["finding_key"] for item in packet["findings"]]
        selected = outcomes(keys) if callable(outcomes) else outcomes
        if edit:
            Path(request.working_directory, "feature.txt").write_text(
                "implemented\nguarded\n", encoding="utf-8"
            )
        report = {
            "schema_version": 1,
            "summary": "repair complete",
            "dispositions": [
                {
                    "finding_key": key,
                    "outcome": outcome,
                    "explanation": f"{outcome} with evidence",
                }
                for key, outcome in zip(keys, selected, strict=True)
            ],
        }
        step.response = _response(
            target,
            text=json.dumps(report),
            session_id="driver-session",
        )

    step.callback = repair
    return step


def _config(
    config_data: dict[str, object],
    *,
    reviewers: list[dict[str, object]] | None = None,
    limit_updates: dict[str, int] | None = None,
) -> bytes:
    data = copy.deepcopy(config_data)
    if reviewers is not None:
        data["reviewers"] = reviewers
    if limit_updates:
        limits = data["limits"]
        assert isinstance(limits, dict)
        limits.update(limit_updates)
    return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


async def _execute(
    root: Path,
    config_bytes: bytes,
    repository: Path,
    driver: ScriptedAgentAdapter,
    reviewers: dict[str, ScriptedAgentAdapter] | None = None,
    *,
    task: str = "Implement the requested change.",
    validator_factory: type[ChangeValidator] = ChangeValidator,
) -> tuple[RunRecord, RunStore, RunHandle]:
    state_name = "s-" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    store = RunStore(root.parent / state_name)
    orchestrator = CodeOnceOrchestrator(
        driver_adapter=driver,
        reviewer_adapters=reviewers,
        change_validator_factory=validator_factory,
    )
    service = DialecticService(store, code_executor=orchestrator)
    handle = service.create_run("code")
    record = await service.execute_code_once(
        handle,
        config_bytes=config_bytes,
        task_bytes=task.encode("utf-8"),
        repository_path=repository,
    )
    return record, store, handle


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_code_001_happy_path_two_reviewers_return_findings(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    driver_target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    claude = AgentTarget(runtime="claude-code", model="claude-model", effort=None)
    grok = AgentTarget(runtime="grok-build", model="grok-model", effort=None)
    driver = ScriptedAgentAdapter(
        driver_target,
        [_edit_step(driver_target), _repair_step(driver_target, ["fixed", "rejected_with_evidence"], edit=True)],
    )
    reviewer_specs = [
        {"id": "first", "runtime": "claude-code", "model": "claude-model", "lens": "correctness"},
        {"id": "second", "runtime": "grok-build", "model": "grok-model", "lens": "security"},
    ]
    first = ScriptedAgentAdapter(claude, [_review_step(claude, [_finding("F1")])])
    second = ScriptedAgentAdapter(grok, [_review_step(grok, [_finding("F2")])])
    record, store, handle = await _execute(
        tmp_path,
        _config(config_data, reviewers=reviewer_specs),
        repo,
        driver,
        {"first": first, "second": second},
    )
    assert record.status == "FINALIZED"
    assert [item.operation for item in driver.invocations] == ["start", "resume"]
    assert len(first.invocations) == len(second.invocations) == 1
    commit_count = int(
        _git(
            store.state_root / "worktrees" / handle.run_id,
            "rev-list",
            "--count",
            "main..HEAD",
        ).stdout
    )
    assert 1 <= commit_count <= 2
    for alias in ("reviewer-a", "reviewer-b"):
        preflight_bytes = (
            handle.path / f"audit/targets/reviewer/{alias}.json"
        ).read_bytes()
        binding_bytes = (
            handle.path / f"audit/capabilities/reviewer/{alias}/review.binding.json"
        ).read_bytes()
        binding = CapabilityBindingArtifact.model_validate_json(
            binding_bytes
        )
        roles = [item.role for item in binding.dynamic_filesystem_identities]
        assert roles == ["neutral_role_dir"]
        for suffix in ("request.json", "attempt.json", "stdout.txt", "stderr.txt"):
            assert (handle.path / f"turns/reviewer/{alias}/review.{suffix}").exists()
        request_bytes = (handle.path / f"turns/reviewer/{alias}/review.request.json").read_bytes()
        attempt = TurnAttemptArtifact.model_validate_json(
            (handle.path / f"turns/reviewer/{alias}/review.attempt.json").read_bytes()
        )
        assert attempt.request_artifact_sha256 == hashlib.sha256(request_bytes).hexdigest()
        assert attempt.target_preflight_artifact_sha256 == hashlib.sha256(
            preflight_bytes
        ).hexdigest()
        assert attempt.capability_binding_artifact_sha256 == hashlib.sha256(
            binding_bytes
        ).hexdigest()
        assert attempt.stdout.persisted_sha256 == hashlib.sha256(b"").hexdigest()
        assert attempt.stderr.persisted_sha256 == hashlib.sha256(b"").hexdigest()
    grok_preflight = TargetPreflightArtifact.model_validate_json(
        (handle.path / "audit/targets/reviewer/reviewer-b.json").read_bytes(),
        strict=True,
    )
    assert (grok_preflight.prompt_transport, grok_preflight.process_lifecycle) == (
        "acp-stdio",
        "per-turn",
    )
    assert not list(handle.path.rglob("*.response.json"))
    summary_text = (handle.path / "summary.md").read_text(encoding="utf-8")
    assert "Repair turn: performed." in summary_text
    assert "post-repair state has not been re-reviewed" in summary_text


@pytest.mark.asyncio
async def test_code_002_all_reviewers_pass_skips_repair(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [])])
    record, store, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.code_outcome == "COMPLETED_NO_FINDINGS"
    assert [call.operation for call in driver.invocations] == ["start", "start"]
    workspace = WorkspaceRecord.model_validate_json((handle.path / "git/workspace.json").read_bytes())
    assert workspace.final_sha == workspace.review_sha
    assert (handle.path / "git/final.diff").read_bytes() == (handle.path / "git/initial.diff").read_bytes()
    summary_text = (handle.path / "summary.md").read_text(encoding="utf-8")
    assert "Repair turn: not performed" in summary_text
    assert "Re-review: not applicable" in summary_text


@pytest.mark.asyncio
async def test_code_003_driver_reviewer_is_a_fresh_session(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [])])
    await _execute(tmp_path, _config(config_data), repo, driver)
    assert [call.operation for call in driver.invocations] == ["start", "start"]
    assert driver.invocations[1].session_id is None


@pytest.mark.asyncio
async def test_code_004_reviewers_start_concurrently(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    driver_target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    targets = [
        AgentTarget(runtime="claude-code", model="claude-model", effort=None),
        AgentTarget(runtime="grok-build", model="grok-model", effort=None),
    ]
    specs = [
        {"id": "a", "runtime": "claude-code", "model": "claude-model", "lens": "a"},
        {"id": "b", "runtime": "grok-build", "model": "grok-model", "lens": "b"},
    ]
    adapters = {
        "a": ScriptedAgentAdapter(targets[0], [_review_step(targets[0], [], delay=0.15)]),
        "b": ScriptedAgentAdapter(targets[1], [_review_step(targets[1], [], delay=0.15)]),
    }
    await _execute(
        tmp_path,
        _config(config_data, reviewers=specs),
        repo,
        ScriptedAgentAdapter(driver_target, [_edit_step(driver_target)]),
        adapters,
    )
    starts = [adapter.invocations[0].started_at for adapter in adapters.values()]
    completions = [adapter.invocations[0].completed_at for adapter in adapters.values()]
    assert max(starts) < min(completions)


@pytest.mark.asyncio
async def test_code_005_review_packets_share_immutable_core(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    driver_target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    targets = [
        AgentTarget(runtime="claude-code", model="claude-model", effort=None),
        AgentTarget(runtime="grok-build", model="grok-model", effort=None),
    ]
    specs = [
        {"id": "a", "runtime": "claude-code", "model": "claude-model", "lens": "lens-a"},
        {"id": "b", "runtime": "grok-build", "model": "grok-model", "lens": "lens-b"},
    ]
    adapters = {
        "a": ScriptedAgentAdapter(targets[0], [_review_step(targets[0], [])]),
        "b": ScriptedAgentAdapter(targets[1], [_review_step(targets[1], [])]),
    }
    await _execute(tmp_path, _config(config_data, reviewers=specs), repo, ScriptedAgentAdapter(driver_target, [_edit_step(driver_target)]), adapters)
    packets = [json.loads(adapter.invocations[0].prompt) for adapter in adapters.values()]
    assert packets[0]["core"] == packets[1]["core"]
    assert packets[0]["lens"] != packets[1]["lens"]


@pytest.mark.asyncio
async def test_code_006_controller_does_not_inject_transcript_or_repository_path(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    authored = "C:/authored/product/path"
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [])])
    _, _, handle = await _execute(
        tmp_path, _config(config_data), repo, driver, task=f"Keep {authored} intact."
    )
    prompt = driver.invocations[1].prompt
    workspace = str((handle.path.parent.parent / "worktrees" / handle.run_id).resolve())
    assert authored in prompt
    assert workspace not in prompt
    assert "summarize your work" not in prompt


@pytest.mark.asyncio
async def test_code_007_reviewer_failure_cancels_and_reaps_peers(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    driver_target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    claude = AgentTarget(runtime="claude-code", model="claude-model", effort=None)
    grok = AgentTarget(runtime="grok-build", model="grok-model", effort=None)
    specs = [
        {"id": "bad", "runtime": "claude-code", "model": "claude-model", "lens": "a"},
        {"id": "slow", "runtime": "grok-build", "model": "grok-model", "lens": "b"},
    ]
    bad = ScriptedAgentAdapter(claude, [ScriptedStep(error=RuntimeError("provider down"))])
    slow = ScriptedAgentAdapter(grok, [_review_step(grok, [], delay=5)])
    driver = ScriptedAgentAdapter(driver_target, [_edit_step(driver_target)])
    record, _, handle = await _execute(tmp_path, _config(config_data, reviewers=specs), repo, driver, {"bad": bad, "slow": slow})
    assert record.failure_kind == "REVIEW_FAILED"
    assert len(driver.invocations) == 1
    slow_attempt = TurnAttemptArtifact.model_validate_json((handle.path / "turns/reviewer/reviewer-b/review.attempt.json").read_bytes())
    assert slow_attempt.attempt_end_reason == "peer-failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["invalid", "per-reviewer", "aggregate"])
async def test_code_008_invalid_or_over_limit_reviews_fail_closed(
    tmp_path: Path, config_data: dict[str, object], variant: str
) -> None:
    repo = _make_repo(tmp_path)
    driver_target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    reviewer_target = AgentTarget(runtime="claude-code", model="claude-model", effort=None)
    if variant == "invalid":
        review = ScriptedStep(response=_response(reviewer_target, text="not-json"))
        updates = None
    else:
        review = _review_step(reviewer_target, [_finding("F1"), _finding("F2")])
        updates = {"max_findings_per_reviewer": 1, "max_total_findings": 1} if variant == "per-reviewer" else {"max_total_findings": 1}
    specs = [{"id": "review", "runtime": "claude-code", "model": "claude-model", "lens": "x"}]
    record, _, _ = await _execute(
        tmp_path,
        _config(config_data, reviewers=specs, limit_updates=updates),
        repo,
        ScriptedAgentAdapter(driver_target, [_edit_step(driver_target)]),
        {"review": ScriptedAgentAdapter(reviewer_target, [review])},
    )
    assert record.failure_kind == "REVIEW_FAILED"


@pytest.mark.asyncio
async def test_code_009_mismatched_review_sha_is_rejected(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    step = _review_step(target, [])
    original = step.callback

    def mismatch(request) -> None:  # type: ignore[no-untyped-def]
        assert original is not None
        original(request)
        assert step.response is not None
        report = json.loads(step.response.text)
        report["head_sha"] = "0" * 40
        step.response = _response(target, text=json.dumps(report), session_id="review")

    step.callback = mismatch
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [_edit_step(target), step]))
    assert record.failure_kind == "REVIEW_FAILED"


@pytest.mark.asyncio
async def test_code_010_repair_feedback_uses_aliases_not_provider_identity(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["rejected_with_evidence"], edit=False)])
    await _execute(tmp_path, _config(config_data), repo, driver)
    prompt = driver.invocations[2].prompt
    assert "reviewer-a/001" in prompt
    assert "codex-model" not in prompt and '"runtime"' not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["omit", "duplicate", "invent"])
async def test_code_011_invalid_disposition_keys_fail_repair(
    tmp_path: Path, config_data: dict[str, object], variant: str
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")

    def keys(existing: list[str]) -> list[str]:
        if variant == "omit":
            return []
        if variant == "duplicate":
            return [existing[0], existing[0]]
        return ["reviewer-z/999"]

    repair = _repair_step(target, lambda existing: ["not_fixed"] * len(keys(existing)), edit=False)

    def malformed(request) -> None:  # type: ignore[no-untyped-def]
        packet = json.loads(request.prompt)
        existing = [item["finding_key"] for item in packet["findings"]]
        selected = keys(existing)
        repair.response = _response(
            target,
            text=json.dumps({
                "schema_version": 1,
                "summary": "bad keys",
                "dispositions": [
                    {"finding_key": key, "outcome": "not_fixed", "explanation": "not fixed"}
                    for key in selected
                ],
            }),
        )

    repair.callback = malformed
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), repair])
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.failure_kind == "REPAIR_FAILED"


@pytest.mark.asyncio
async def test_code_012_fixed_findings_create_repair_commit(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["fixed"], edit=True)])
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.code_outcome == "COMPLETED_AFTER_REPAIR"
    assert int(_git(Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"]), "rev-list", "--count", "main..HEAD").stdout) == 2


@pytest.mark.asyncio
async def test_code_013_all_rebuttals_allow_empty_repair_delta(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["rejected_with_evidence"], edit=False)])
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.code_outcome == "COMPLETED_WITH_REBUTTALS"
    assert (handle.path / "git/repair.delta.diff").read_bytes() == b""


@pytest.mark.asyncio
async def test_code_014_not_fixed_is_reported_as_unresolved(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["not_fixed"], edit=False)])
    record, store, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    summary = SummaryRecord.model_validate_json((handle.path / "summary.json").read_bytes())
    assert record.code_outcome == "COMPLETED_WITH_UNRESOLVED_FINDINGS"
    assert summary.unresolved_items == ["reviewer-a/001"]
    assert DialecticService(store).get_result(handle.run_id).unresolved_items == [
        "reviewer-a/001"
    ]
    assert "reviewer-a/001" in (handle.path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_code_015_no_initial_changes_runs_no_reviewer(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [ScriptedStep(response=_response(target))])
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.failure_kind == "NO_CHANGES"
    assert len(driver.invocations) == 1


@pytest.mark.asyncio
async def test_code_016_diff_overflow_stops_before_review_and_commit(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target, content=b"x" * 500)])
    record, _, handle = await _execute(tmp_path, _config(config_data, limit_updates={"max_diff_bytes": 80}), repo, driver)
    assert record.failure_kind == "DIFF_TOO_LARGE"
    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    assert int(_git(workspace, "rev-list", "--count", "main..HEAD").stdout) == 0


@pytest.mark.asyncio
async def test_code_017_dirty_original_fails_before_worktree_creation(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, store, _ = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, []))
    assert record.failure_kind == "UNSUPPORTED_REPOSITORY"
    assert not (store.state_root / "worktrees").exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_code_018_full_git_path_preserves_original_checkout(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    baseline = {
        "head": _git(repo, "rev-parse", "HEAD").stdout,
        "branch": _git(repo, "symbolic-ref", "--short", "HEAD").stdout,
        "status": _git(repo, "status", "--porcelain=v1", "-z").stdout,
        "file": (repo / "app.txt").read_bytes(),
    }
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [])]))
    assert record.status == "FINALIZED"
    assert _git(repo, "rev-parse", "HEAD").stdout == baseline["head"]
    assert _git(repo, "symbolic-ref", "--short", "HEAD").stdout == baseline["branch"]
    assert _git(repo, "status", "--porcelain=v1", "-z").stdout == baseline["status"]
    assert (repo / "app.txt").read_bytes() == baseline["file"]
    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    assert (workspace / "feature.txt").exists()


@pytest.mark.asyncio
async def test_code_019_exact_call_count_has_no_second_review(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["fixed"], edit=True)])
    await _execute(tmp_path, _config(config_data), repo, driver)
    assert [call.operation for call in driver.invocations] == ["start", "start", "resume"]


@pytest.mark.asyncio
async def test_code_020_failure_preserves_partial_isolated_worktree(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [_edit_step(target), ScriptedStep(error=RuntimeError("review failed"))]))
    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    assert record.failure_kind == "REVIEW_FAILED"
    assert workspace.exists() and (workspace / "feature.txt").exists()
    assert _load_json(handle.path / "summary.json")["artifact_paths"]["workspace"] == "git/workspace.json"


@pytest.mark.asyncio
async def test_code_021_duplicate_local_ids_get_distinct_global_keys(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    driver_target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    claude = AgentTarget(runtime="claude-code", model="claude-model", effort=None)
    grok = AgentTarget(runtime="grok-build", model="grok-model", effort=None)
    specs = [
        {"id": "a", "runtime": "claude-code", "model": "claude-model", "lens": "a"},
        {"id": "b", "runtime": "grok-build", "model": "grok-model", "lens": "b"},
    ]
    driver = ScriptedAgentAdapter(driver_target, [_edit_step(driver_target), _repair_step(driver_target, ["not_fixed", "not_fixed"], edit=False)])
    _, _, handle = await _execute(tmp_path, _config(config_data, reviewers=specs), repo, driver, {
        "a": ScriptedAgentAdapter(claude, [_review_step(claude, [_finding("F1")])]),
        "b": ScriptedAgentAdapter(grok, [_review_step(grok, [_finding("F1")])]),
    })
    keys = [item["finding_key"] for item in _load_json(handle.path / "feedback.json")["findings"]]
    assert keys == ["reviewer-a/001", "reviewer-b/001"]


@pytest.mark.asyncio
async def test_code_022_mixed_dispositions_use_unresolved_precedence(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [
        _edit_step(target),
        _review_step(target, [_finding("A"), _finding("B"), _finding("C")]),
        _repair_step(target, ["fixed", "rejected_with_evidence", "not_fixed"], edit=True),
    ])
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.code_outcome == "COMPLETED_WITH_UNRESOLVED_FINDINGS"
    summary = (handle.path / "summary.md").read_text(encoding="utf-8")
    assert "reviewer-a/003" in summary and "Rebuttals" in summary


@pytest.mark.asyncio
async def test_code_023_fixed_claim_requires_nonempty_repair_delta(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["fixed"], edit=False)])
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.failure_kind == "REPAIR_FAILED"


@pytest.mark.asyncio
async def test_code_024_binary_initial_change_is_unsupported(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [_edit_step(target, name="image.bin", content=b"\x00\xff\x00")]))
    assert record.failure_kind == "UNSUPPORTED_CHANGE"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["sparse", "submodule", "lfs", "filter"])
async def test_code_025_unsupported_repository_features_fail_preflight(
    tmp_path: Path, config_data: dict[str, object], variant: str
) -> None:
    repo = _make_repo(tmp_path)
    if variant == "sparse":
        _git(repo, "config", "core.sparseCheckout", "true")
    elif variant == "submodule":
        source = tmp_path / "submodule-source"
        source.mkdir()
        _git(source, "init", "-b", "main")
        _git(source, "config", "user.name", "Test User")
        _git(source, "config", "user.email", "test@example.invalid")
        (source / "module.txt").write_text("module\n", encoding="utf-8")
        _git(source, "add", "module.txt")
        _git(source, "commit", "-m", "module")
        _git(repo, "clone", str(source), "vendor")
        module_sha = _git(source, "rev-parse", "HEAD").stdout.decode("ascii").strip()
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{module_sha},vendor")
        _git(repo, "commit", "-m", "tracked submodule")
    else:
        filter_name = "lfs" if variant == "lfs" else "blocked"
        (repo / ".gitattributes").write_text(
            f"app.txt filter={filter_name}\n", encoding="utf-8"
        )
        _git(repo, "add", ".gitattributes")
        _git(repo, "commit", "-m", "filtered")
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, store, handle = await _execute(
        tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [])
    )
    assert record.failure_kind == "UNSUPPORTED_REPOSITORY"
    assert not (store.state_root / "worktrees" / handle.run_id).exists()


@pytest.mark.asyncio
async def test_code_026_controller_git_disables_repository_hooks(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    sentinel = repo / "hook-ran"
    hooks = repo / ".git" / "hooks"
    hook = hooks / "post-commit"
    hook.write_text(f"#!/bin/sh\necho ran > '{sentinel.as_posix()}'\n", encoding="utf-8")
    if os.name != "nt":
        hook.chmod(0o755)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [])]))
    assert record.status == "FINALIZED"
    assert not sentinel.exists()


def test_code_027_git_runner_pins_external_command_and_signing_defenses(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    runner = GitRunner(hooks)
    runner.run(["status", "--porcelain=v1", "-z"], cwd=repo)
    command = runner.history[0]
    assert f"core.hooksPath={hooks.resolve()}" in command
    assert "core.fsmonitor=false" in command
    assert "commit.gpgSign=false" in command
    assert "core.pager=cat" in command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "expected_kind"),
    [
        ("nonzero", "DRIVER_FAILED"),
        ("timeout", "DRIVER_FAILED"),
        ("malformed-envelope", "DRIVER_FAILED"),
        ("no-session", "DRIVER_FAILED"),
        ("model-mismatch", "MODEL_MISMATCH"),
    ],
)
async def test_code_028_initial_driver_failure_variants_run_no_reviewer(
    tmp_path: Path,
    config_data: dict[str, object],
    variant: str,
    expected_kind: str,
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    if variant == "nonzero":
        step = ScriptedStep(error=AgentProcessError(17, "native process exited nonzero"))
        limits = None
    elif variant == "timeout":
        step = ScriptedStep(response=_response(target), delay_seconds=2)
        limits = {"agent_turn_seconds": 1}
    elif variant == "malformed-envelope":
        step = None
        limits = None
    elif variant == "no-session":
        step = _edit_step(target)
        step.response = _response(target, session_id=None)
        limits = None
    else:
        step = ScriptedStep(response=_response(target, actual_model="different-model"))
        limits = None
    if step is None:
        class MalformedEnvelopeAdapter(ScriptedAgentAdapter):
            async def start(self, request):  # type: ignore[no-untyped-def]
                self.invocations.append(object())  # type: ignore[arg-type]
                return object()

        driver = MalformedEnvelopeAdapter(target, [])
    else:
        driver = ScriptedAgentAdapter(target, [step])
    record, _, handle = await _execute(
        tmp_path, _config(config_data, limit_updates=limits), repo, driver
    )
    assert record.failure_kind == expected_kind
    assert len(driver.invocations) == 1
    attempt = TurnAttemptArtifact.model_validate_json(
        (handle.path / "turns/driver/driver/initial.attempt.json").read_bytes()
    )
    if variant == "no-session":
        assert attempt.response is not None
    else:
        assert attempt.response is None
    if variant == "nonzero":
        assert attempt.process_origin == "spawned-for-attempt"
        assert attempt.process_exit_code == 17
        assert attempt.attempt_end_reason == "agent-failed"
    if variant == "malformed-envelope":
        assert attempt.process_origin == "spawned-for-attempt"
        assert attempt.process_exit_code == 0
        assert attempt.attempt_end_reason == "agent-failed"


def test_code_029_second_repository_lock_names_holder(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip())
    identity = resolve_repository_identity(common)
    first = RepositoryLock(tmp_path / "locks", identity, "first-run").acquire()
    try:
        with pytest.raises(RepositoryBusyError, match="first-run"):
            RepositoryLock(tmp_path / "locks", identity, "second-run").acquire()
    finally:
        first.release()


@pytest.mark.asyncio
async def test_code_030_workflow_wall_clock_expires_during_reviews(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [], delay=2)])
    record, _, _ = await _execute(tmp_path, _config(config_data, limit_updates={"code_run_seconds": 1}), repo, driver)
    assert record.status == "TIMED_OUT"
    assert len(driver.invocations) == 2


@pytest.mark.asyncio
async def test_code_031_oversized_packet_starts_no_reviewer(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target)])
    record, _, _ = await _execute(
        tmp_path,
        _config(config_data, limit_updates={"max_packet_bytes": 2_000}),
        repo,
        driver,
    )
    assert record.failure_kind == "PACKET_TOO_LARGE"
    assert len(driver.invocations) == 1


@pytest.mark.asyncio
async def test_code_032_driver_prompt_warns_about_ignored_environment_artifacts(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [])])
    await _execute(tmp_path, _config(config_data), repo, driver)
    prompt = driver.invocations[0].prompt
    assert ".venv" in prompt and "node_modules" in prompt and "Do not repair environment" in prompt


@pytest.mark.asyncio
async def test_code_033_finding_outside_diff_is_accepted(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding(file="outside.py")]), _repair_step(target, ["not_fixed"], edit=False)])
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.status == "FINALIZED"
    assert _load_json(handle.path / "feedback.json")["findings"][0]["finding"]["file"] == "outside.py"


@pytest.mark.asyncio
async def test_code_034_new_filter_matched_path_fails_before_commit(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")

    def filtered(request) -> None:  # type: ignore[no-untyped-def]
        worktree = Path(request.working_directory)
        (worktree / ".gitattributes").write_text("generated.txt filter=blocked\n", encoding="utf-8")
        (worktree / "generated.txt").write_text("data\n", encoding="utf-8")

    driver = ScriptedAgentAdapter(target, [ScriptedStep(response=_response(target), callback=filtered)])
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.failure_kind == "UNSUPPORTED_CHANGE"
    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    assert int(_git(workspace, "rev-list", "--count", "main..HEAD").stdout) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "expected_kind"),
    [
        ("binary", "UNSUPPORTED_CHANGE"),
        ("gitlink", "UNSUPPORTED_CHANGE"),
        ("filter", "UNSUPPORTED_CHANGE"),
        ("invalid-content", "UNSUPPORTED_CHANGE"),
        pytest.param(
            "invalid-path",
            "UNSUPPORTED_CHANGE",
            marks=pytest.mark.skipif(os.name == "nt", reason="Win32 paths are Unicode"),
        ),
        ("over-diff", "DIFF_TOO_LARGE"),
    ],
)
async def test_code_035_repair_reruns_all_shared_change_validation(
    tmp_path: Path,
    config_data: dict[str, object],
    variant: str,
    expected_kind: str,
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    repair = _repair_step(target, ["fixed"], edit=False)
    original = repair.callback

    def introduce_invalid_repair(request) -> None:  # type: ignore[no-untyped-def]
        assert original is not None
        original(request)
        worktree = Path(request.working_directory)
        if variant == "binary":
            (worktree / "bad.bin").write_bytes(b"\x00\xff")
        elif variant == "gitlink":
            nested = worktree / "nested"
            nested.mkdir()
            _git(nested, "init")
            _git(nested, "config", "user.name", "Test User")
            _git(nested, "config", "user.email", "test@example.invalid")
            (nested / "nested.txt").write_text("nested\n", encoding="utf-8")
            _git(nested, "add", "nested.txt")
            _git(nested, "commit", "-m", "nested")
            _git(worktree, "add", "nested")
        elif variant == "filter":
            (worktree / ".gitattributes").write_text(
                "generated.txt filter=blocked\n", encoding="utf-8"
            )
            (worktree / "generated.txt").write_text("filtered\n", encoding="utf-8")
        elif variant == "invalid-content":
            (worktree / "latin1.txt").write_bytes(b"invalid-\xff\n")
        elif variant == "invalid-path":
            raw_path = os.path.join(os.fsencode(worktree), b"latin1-\xff.txt")
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, b"valid utf8\n")
            os.close(descriptor)
        else:
            (worktree / "large.txt").write_text("line\n" * 2_000, encoding="utf-8")

    repair.callback = introduce_invalid_repair
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), repair])
    limits = {"max_diff_bytes": 1_024} if variant == "over-diff" else None
    record, _, handle = await _execute(
        tmp_path, _config(config_data, limit_updates=limits), repo, driver
    )
    assert record.failure_kind == expected_kind
    assert [call.operation for call in driver.invocations] == ["start", "start", "resume"]
    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    assert int(_git(workspace, "rev-list", "--count", "main..HEAD").stdout) == 1


@pytest.mark.asyncio
async def test_code_036_embedded_repository_gitlink_is_rejected(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")

    def embedded(request) -> None:  # type: ignore[no-untyped-def]
        nested = Path(request.working_directory, "nested")
        nested.mkdir()
        _git(nested, "init")

    record, _, _ = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, [ScriptedStep(response=_response(target), callback=embedded)]))
    assert record.failure_kind == "UNSUPPORTED_CHANGE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "invalid-content",
        pytest.param(
            "invalid-path",
            marks=pytest.mark.skipif(os.name == "nt", reason="Win32 paths are Unicode"),
        ),
    ],
)
async def test_code_037_strict_utf8_paths_and_content(
    tmp_path: Path, config_data: dict[str, object], variant: str
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    if variant == "valid":
        name = "unicod\u00e9.txt" if os.name == "nt" else "unicod\u00e9\tfile.txt"
        step = _edit_step(target, name=name, content="caf\u00e9\n".encode())
    elif variant == "invalid-content":
        step = _edit_step(target, name="latin1.txt", content=b"caf\xe9\n")
    else:
        def invalid_path(request) -> None:  # type: ignore[no-untyped-def]
            raw_path = os.path.join(
                os.fsencode(request.working_directory), b"latin1-\xff.txt"
            )
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, b"valid utf8\n")
            os.close(descriptor)

        step = ScriptedStep(response=_response(target), callback=invalid_path)
    driver = ScriptedAgentAdapter(
        target,
        [step, _review_step(target, [])],
    )
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    if variant != "valid":
        assert record.failure_kind == "UNSUPPORTED_CHANGE"
        assert len(driver.invocations) == 1
        return
    diff = (handle.path / "git/initial.diff").read_bytes()
    assert record.status == "FINALIZED"
    diff.decode("utf-8", errors="strict")
    expected = (handle.path / "git/initial.diff.sha256").read_text().strip()
    assert hashlib.sha256(diff).hexdigest() == expected


@pytest.mark.asyncio
async def test_code_038_ignored_bytecode_is_excluded_from_snapshot(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore caches")
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")

    def edit(request) -> None:  # type: ignore[no-untyped-def]
        worktree = Path(request.working_directory)
        (worktree / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        cache = worktree / "__pycache__"
        cache.mkdir()
        (cache / "source.pyc").write_bytes(b"bytecode")

    driver = ScriptedAgentAdapter(target, [ScriptedStep(response=_response(target), callback=edit), _review_step(target, [])])
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    diff = (handle.path / "git/initial.diff").read_text(encoding="utf-8")
    assert record.status == "FINALIZED"
    assert "source.py" in diff and "pyc" not in diff


def test_code_039_codex_environment_construction_withholds_credentials(tmp_path: Path) -> None:
    for name in ("worktree", "git", "original", "state", "scratch", "control", "tmp"):
        (tmp_path / name).mkdir()
    fixture = CodexConstructionFixture(
        credential_environment_names=("API_SECRET",),
        non_secret_environment_names=("PATH", "TEMP"),
        saved_auth_paths=(tmp_path / "auth.json",),
    )
    construction = build_codex_driver_construction(
        fixture=fixture,
        source_environment={"API_SECRET": "supersecret", "PATH": "bin", "TEMP": "temp", "OTHER": "no"},
        worktree=tmp_path / "worktree",
        git_common_dir=tmp_path / "git",
        original_worktree=tmp_path / "original",
        state_root=tmp_path / "state",
        scratch_root=tmp_path / "scratch",
        scratch_control=tmp_path / "control",
        scratch_tmp=tmp_path / "tmp",
    )
    assert set(construction.trusted_environment) == {"API_SECRET", "PATH", "TEMP"}
    assert construction.child_environment_policy["exclude"] == ["API_SECRET"]
    assert "--sandbox" not in construction.arguments
    assert construction.arguments.count("-c") > 0
    assert all(not item.startswith("-cprofile=") for item in construction.arguments)
    serialized = json.dumps({"policy": construction.child_environment_policy, "profile": construction.concrete_profile})
    assert "supersecret" not in serialized
    filesystem = construction.concrete_profile["permissions"]["dialectic-driver"]["filesystem"]
    assert filesystem[str((tmp_path / "auth.json").resolve())] == "deny"


@pytest.mark.asyncio
async def test_code_040_driver_bindings_recreate_exact_scratch_roles_and_identity(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [_finding()]), _repair_step(target, ["not_fixed"], edit=False)])
    record, store, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.status == "FINALIZED"
    initial = CapabilityBindingArtifact.model_validate_json((handle.path / "audit/capabilities/driver/driver/initial.binding.json").read_bytes())
    repair = CapabilityBindingArtifact.model_validate_json((handle.path / "audit/capabilities/driver/driver/repair.binding.json").read_bytes())
    initial_by_role = {item.role: item for item in initial.dynamic_filesystem_identities}
    repair_by_role = {item.role: item for item in repair.dynamic_filesystem_identities}
    scratch_roles = {"turn_scratch_root", "turn_scratch_control", "turn_scratch_tmp"}
    assert scratch_roles.issubset(initial_by_role) and scratch_roles.issubset(repair_by_role)
    for role in scratch_roles:
        assert initial_by_role[role].path_sha256 == repair_by_role[role].path_sha256
        assert initial_by_role[role].filesystem_identity != repair_by_role[role].filesystem_identity
    assert initial.profile_template_sha256 == repair.profile_template_sha256
    assert initial.concrete_profile_sha256 == repair.concrete_profile_sha256
    assert initial.target_preflight_artifact_sha256 == repair.target_preflight_artifact_sha256
    assert initial.capability_attestation_sha256 == repair.capability_attestation_sha256
    initial_semantics = initial.model_dump(mode="json")
    repair_semantics = repair.model_dump(mode="json")
    initial_semantics.pop("binding_id")
    repair_semantics.pop("binding_id")
    for semantics in (initial_semantics, repair_semantics):
        for identity in semantics["dynamic_filesystem_identities"]:
            if identity["role"] in scratch_roles:
                identity["filesystem_identity"] = "recreated-scratch-identity"
    assert initial_semantics == repair_semantics

    broken = initial.model_copy(update={"dynamic_filesystem_identities": [item for item in initial.dynamic_filesystem_identities if item.role != "turn_scratch_tmp"]})
    with pytest.raises(ValidationError):
        CapabilityBindingArtifact.model_validate(broken.model_dump())
    with pytest.raises(CodexPolicyError):
        build_codex_driver_construction(
            fixture=CodexConstructionFixture((), (), ()),
            source_environment={},
            worktree=Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"]),
            git_common_dir=repo / ".git",
            original_worktree=repo,
            state_root=handle.path.parent.parent,
            scratch_root=tmp_path,
            scratch_control=tmp_path,
            scratch_tmp=tmp_path,
            managed_policy={"sandbox_mode": "workspace-write"},
        )

    policy_root = tmp_path / "policy"
    policy_paths = {
        name: policy_root / name
        for name in ("worktree", "git", "original", "state", "scratch", "control", "tmp")
    }
    for path in policy_paths.values():
        path.mkdir(parents=True)
    auth_path = policy_root / "saved-auth.json"
    construction = build_codex_driver_construction(
        fixture=CodexConstructionFixture((), (), (auth_path,)),
        source_environment={},
        worktree=policy_paths["worktree"],
        git_common_dir=policy_paths["git"],
        original_worktree=policy_paths["original"],
        state_root=policy_paths["state"],
        scratch_root=policy_paths["scratch"],
        scratch_control=policy_paths["control"],
        scratch_tmp=policy_paths["tmp"],
    )
    expected_filesystem = {
        ":root": "deny",
        ":minimal": "read",
        ":tmpdir": "deny",
        ":slash_tmp": "deny",
        "<isolated_worktree>": "write",
        "<isolated_worktree:.git>": "read",
        "<isolated_worktree:.codex>": "read",
        "<git_common_dir>": "read",
        "<original_worktree>": "deny",
        "<state_root>": "deny",
        "<turn_scratch_root>": "read",
        "<turn_scratch_control>": "read",
        "<turn_scratch_tmp>": "write",
        str(auth_path.resolve()): "deny",
        str(Path(tempfile.gettempdir()).resolve()): "deny",
    }
    assert construction.profile_template == {
        "approval_policy": "never",
        "apps": {"_default": {"enabled": False}},
        "default_permissions": "dialectic-driver",
        "features": {"multi_agent": False},
        "mcp_servers": {},
        "permissions": {
            "dialectic-driver": {
                "filesystem": expected_filesystem,
                "network": {"enabled": False},
            }
        },
        "projects": {"<isolated_worktree>": {"trust_level": "untrusted"}},
        "shell_environment_policy": {
            "inherit": "core",
            "ignore_default_excludes": False,
            "experimental_use_profile": False,
            "exclude": [],
            "set": {"GIT_OPTIONAL_LOCKS": "0"},
        },
        "web_search": "disabled",
    }
    assert construction.arguments[:3] == ("exec", "--ignore-user-config", "--ignore-rules")
    assert construction.arguments[3] == "--strict-config"
    assert construction.arguments[-1] == "-"
    override_keys = [
        construction.arguments[index + 1].split("=", 1)[0]
        for index, value in enumerate(construction.arguments[:-1])
        if value == "-c"
    ]
    assert override_keys == sorted(construction.concrete_profile)
    serialized_profile = json.dumps(construction.concrete_profile, sort_keys=True)
    assert "sandbox_mode" not in serialized_profile and "--sandbox" not in construction.arguments
    assert construction.concrete_profile["default_permissions"] == "dialectic-driver"
    concrete_filesystem = construction.concrete_profile["permissions"]["dialectic-driver"]["filesystem"]
    assert concrete_filesystem[str(auth_path.resolve())] == "deny"

    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    scratch = TurnWorkspace.create(workspace)
    dynamic_paths = {
        "isolated_worktree": workspace,
        "git_common_dir": repo / ".git",
        "original_worktree": repo,
        "state_root": store.state_root,
        "turn_scratch_root": scratch.root,
        "turn_scratch_control": scratch.control,
        "turn_scratch_tmp": scratch.temporary,
    }
    fixture = CapabilityFixture(
        probe_ids=("offline-construction",),
        dynamic_roles=tuple(dynamic_paths),
        template={
            "access_mode": "driver-write",
            "filesystem": [
                {"role": role, "path": {"dynamic_path": role}}
                for role in dynamic_paths
            ],
        },
    )
    preflight_bytes = (handle.path / "audit/targets/driver/driver.json").read_bytes()
    attestation_bytes = store.read_capability_attestation(
        initial.capability_attestation_sha256
    )
    assert attestation_bytes is not None
    attestation = CapabilityAttestationArtifact.model_validate_json(attestation_bytes)
    concrete = {
        "access_mode": "driver-write",
        "filesystem": [
            {"role": role, "path": str(path.resolve())}
            for role, path in dynamic_paths.items()
        ],
    }
    live_binding = build_capability_binding(
        binding_id="live-code-040",
        role="driver",
        target_id="driver",
        access_mode="driver-write",
        target_preflight_bytes=preflight_bytes,
        attestation_bytes=attestation_bytes,
        attestation=attestation,
        fixture=fixture,
        dynamic_paths=dynamic_paths,
        supplied_concrete_profile=concrete,
    )
    with pytest.raises(CapabilityEvidenceError, match="role set"):
        build_capability_binding(
            binding_id="missing-code-040",
            role="driver",
            target_id="driver",
            access_mode="driver-write",
            target_preflight_bytes=preflight_bytes,
            attestation_bytes=attestation_bytes,
            attestation=attestation,
            fixture=fixture,
            dynamic_paths={
                role: path for role, path in dynamic_paths.items() if role != "turn_scratch_tmp"
            },
            supplied_concrete_profile=concrete,
        )
    with pytest.raises(CapabilityEvidenceError, match="role set"):
        build_capability_binding(
            binding_id="surplus-code-040",
            role="driver",
            target_id="driver",
            access_mode="driver-write",
            target_preflight_bytes=preflight_bytes,
            attestation_bytes=attestation_bytes,
            attestation=attestation,
            fixture=fixture,
            dynamic_paths={**dynamic_paths, "neutral_role_dir": tmp_path},
            supplied_concrete_profile=concrete,
        )
    substituted_paths = dict(dynamic_paths)
    substituted_paths["turn_scratch_control"] = scratch.temporary
    substituted_paths["turn_scratch_tmp"] = scratch.control
    with pytest.raises(CapabilityEvidenceError, match="canonical template"):
        build_capability_binding(
            binding_id="relabelled-code-040",
            role="driver",
            target_id="driver",
            access_mode="driver-write",
            target_preflight_bytes=preflight_bytes,
            attestation_bytes=attestation_bytes,
            attestation=attestation,
            fixture=fixture,
            dynamic_paths=substituted_paths,
            supplied_concrete_profile=concrete,
        )
    identity_tamper = live_binding.model_copy(
        update={
            "dynamic_filesystem_identities": [
                item.model_copy(update={"filesystem_identity": "wrong-identity"})
                if item.role == "turn_scratch_tmp"
                else item
                for item in live_binding.dynamic_filesystem_identities
            ]
        }
    )
    with pytest.raises(CapabilityEvidenceError, match="identity changed"):
        validate_binding_identities(
            identity_tamper,
            dynamic_paths=dynamic_paths,
            platform_backend=attestation.platform_backend,
        )
    scratch.verify_and_cleanup(LimitsSpec.model_validate(config_data["limits"]))


def test_code_041_equivalent_repository_spellings_share_lock_identity(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    identity_a = resolve_repository_identity(repo / ".git")
    identity_b = resolve_repository_identity(repo / "." / ".git")
    assert identity_a.lock_identity_sha256 == identity_b.lock_identity_sha256


@pytest.mark.asyncio
async def test_code_042_model_authored_self_identification_survives_feedback(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    finding = _finding()
    finding["claim"] = "I am Provider Example and found a problem."
    driver = ScriptedAgentAdapter(target, [_edit_step(target), _review_step(target, [finding]), _repair_step(target, ["not_fixed"], edit=False)])
    _, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert "Provider Example" in (handle.path / "feedback.json").read_text(encoding="utf-8")


def test_code_043_post_commit_race_fails_exact_byte_confirmation(tmp_path: Path, limits: dict[str, int]) -> None:
    repo = _make_repo(tmp_path)
    state = tmp_path.parent / "s-code043"
    store = RunStore(state)
    handle = store.bootstrap_run("code")
    hooks = handle.path / "hooks"
    hooks.mkdir()
    runner = GitRunner(hooks)
    baseline = GitWorkspace(runner, state).preflight(repo)
    workspace = GitWorkspace(runner, state).create_linked_worktree(baseline, handle.run_id)
    (workspace.path / "feature.txt").write_text("valid\n", encoding="utf-8")

    def mutate(path: Path) -> None:
        (path / "feature.txt").write_text("raced\n", encoding="utf-8")

    validator = ChangeValidator(runner=runner, store=store, handle=handle, workspace=workspace, limits=LimitsSpec.model_validate(limits), after_commit=mutate)
    with pytest.raises(GitWorkflowError) as caught:
        validator.validate_initial()
    assert caught.value.kind == "INTERNAL_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "expected_kind"),
    [
        pytest.param(
            "control-symlink",
            "INTERNAL_ERROR",
            marks=pytest.mark.skipif(os.name == "nt", reason="unprivileged Win32 symlinks vary"),
        ),
        ("control-hardlink", "INTERNAL_ERROR"),
        pytest.param(
            "fifo",
            "INTERNAL_ERROR",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO contract"),
        ),
        pytest.param(
            "socket",
            "INTERNAL_ERROR",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX socket-path contract"),
        ),
        pytest.param(
            "posix-rename-race",
            "PROCESS_CLEANUP_FAILED",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative race contract"),
        ),
        pytest.param(
            "windows-junction",
            "INTERNAL_ERROR",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows reparse contract"),
        ),
        ("cleanup-failure", "PROCESS_CLEANUP_FAILED"),
    ],
)
async def test_code_044_reserved_workspace_attacks_fail_closed_before_git(
    tmp_path: Path,
    config_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    expected_kind: str,
) -> None:
    repo = _make_repo(tmp_path)
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside-secret", encoding="utf-8")
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    outside_sentinel = outside_directory / "sentinel.txt"
    outside_sentinel.write_text("outside-directory-secret", encoding="utf-8")
    race_directory: list[Path] = []

    def attack(request) -> None:  # type: ignore[no-untyped-def]
        scratch_root = Path(request.working_directory) / ".dialectic-turn"
        output = scratch_root / "control" / "output.json"
        temporary = scratch_root / "tmp"
        if variant == "control-symlink":
            output.unlink()
            os.symlink(outside_file, output)
        elif variant == "control-hardlink":
            output.unlink()
            os.link(outside_file, output)
        elif variant == "fifo":
            os.mkfifo(temporary / "hostile.fifo")
        elif variant == "socket":
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_socket:
                unix_socket.bind(str(temporary / "hostile.socket"))
        elif variant == "posix-rename-race":
            race = temporary / "race"
            race.mkdir()
            (race / "owned.txt").write_text("owned\n", encoding="utf-8")
            race_directory.append(race)
        elif variant == "windows-junction":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(temporary / "junction"), str(outside_directory)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")

    if variant == "cleanup-failure":
        def fail_cleanup(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise ScratchCleanupTimeout("forced cleanup timeout")

        monkeypatch.setattr("dialectic.turn_workspace.cleanup_reserved_tree", fail_cleanup)
    elif variant == "posix-rename-race":
        def race_cleanup(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            race = race_directory[0]
            held = race.with_name("race-held")
            race.rename(held)
            os.symlink(outside_directory, race, target_is_directory=True)
            raise ScratchContainmentError("simulated rename race")

        monkeypatch.setattr("dialectic.scratch._cleanup_directory_fd", race_cleanup)

    driver = ScriptedAgentAdapter(
        target, [ScriptedStep(response=_response(target), callback=attack)]
    )
    record, _, handle = await _execute(tmp_path, _config(config_data), repo, driver)
    assert record.failure_kind == expected_kind
    assert outside_file.read_text(encoding="utf-8") == "outside-secret"
    assert outside_sentinel.read_text(encoding="utf-8") == "outside-directory-secret"
    assert not (handle.path / "git/initial.diff").exists()
    workspace = Path(_load_json(handle.path / "git/workspace.json")["dialectic_worktree"])
    assert int(_git(workspace, "rev-list", "--count", "main..HEAD").stdout) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        "nested-rename-delete-duplicate",
        "regular-over-limit",
        "sparse-over-limit",
        "aggregate-over-limit",
        "empty-file",
        pytest.param(
            "added-symlink",
            marks=pytest.mark.skipif(os.name == "nt", reason="unprivileged Win32 symlinks vary"),
        ),
        pytest.param(
            "modified-symlink",
            marks=pytest.mark.skipif(os.name == "nt", reason="unprivileged Win32 symlinks vary"),
        ),
        "multiply-linked",
    ],
)
async def test_code_045_changed_leaf_enumeration_and_pre_staging_bounds(
    tmp_path: Path, config_data: dict[str, object], variant: str
) -> None:
    repo = _make_repo(tmp_path)
    if variant == "nested-rename-delete-duplicate":
        (repo / "delete.txt").write_text("delete\n", encoding="utf-8")
        (repo / "rename.txt").write_text("rename\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fixtures")
    elif variant == "modified-symlink":
        (repo / "linked.txt").write_text("ordinary file\n", encoding="utf-8")
        _git(repo, "add", "linked.txt")
        _git(repo, "commit", "-m", "link fixture")
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    outside = tmp_path / "outside-candidate.txt"
    outside.write_text("outside candidate\n", encoding="utf-8")

    def changes(request) -> None:  # type: ignore[no-untyped-def]
        worktree = Path(request.working_directory)
        if variant == "nested-rename-delete-duplicate":
            (worktree / "delete.txt").unlink()
            (worktree / "rename.txt").rename(worktree / "renamed.txt")
            nested = worktree / "nested" / "deep"
            nested.mkdir(parents=True)
            leaf = nested / "leaf.txt"
            leaf.write_text("one\n", encoding="utf-8")
            _git(worktree, "add", "nested/deep/leaf.txt")
            leaf.write_text("two\n", encoding="utf-8")
        elif variant == "regular-over-limit":
            (worktree / "large.txt").write_bytes(b"x" * 9)
        elif variant == "sparse-over-limit":
            with (worktree / "sparse.txt").open("wb") as sparse:
                sparse.seek(8)
                sparse.write(b"x")
        elif variant == "aggregate-over-limit":
            (worktree / "first.txt").write_bytes(b"x" * 6)
            (worktree / "second.txt").write_bytes(b"y" * 6)
        elif variant == "empty-file":
            (worktree / "empty.txt").touch()
            (worktree / "nonempty.txt").write_text("content\n", encoding="utf-8")
        elif variant == "added-symlink":
            os.symlink(outside, worktree / "linked.txt")
        elif variant == "modified-symlink":
            (worktree / "linked.txt").unlink()
            os.symlink(outside, worktree / "linked.txt")
        else:
            os.link(outside, worktree / "linked.txt")

    validated_changes = []
    validation_runners: list[GitRunner] = []

    class RecordingValidator(ChangeValidator):
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            super().__init__(**kwargs)
            validation_runners.append(self.runner)

        def validate_initial(self):  # type: ignore[no-untyped-def]
            result = super().validate_initial()
            validated_changes.append(result)
            return result

    limit_updates = None
    if variant in {"regular-over-limit", "sparse-over-limit"}:
        limit_updates = {
            "max_changed_regular_file_bytes": 8,
            "max_candidate_change_bytes": 16,
        }
    elif variant == "aggregate-over-limit":
        limit_updates = {
            "max_changed_regular_file_bytes": 8,
            "max_candidate_change_bytes": 10,
        }
    objects_before = _git(repo, "count-objects", "-v").stdout
    driver = ScriptedAgentAdapter(
        target,
        [ScriptedStep(response=_response(target), callback=changes), _review_step(target, [])],
    )
    record, _, handle = await _execute(
        tmp_path,
        _config(config_data, limit_updates=limit_updates),
        repo,
        driver,
        validator_factory=RecordingValidator,
    )
    successful = variant in {"nested-rename-delete-duplicate", "empty-file"}
    if successful:
        assert record.status == "FINALIZED"
        diff = (handle.path / "git/initial.diff").read_text(encoding="utf-8")
        if variant == "nested-rename-delete-duplicate":
            assert all(
                name in diff
                for name in ("delete.txt", "rename.txt", "renamed.txt", "nested/deep/leaf.txt")
            )
            assert validated_changes[0].changed_paths == tuple(
                sorted(set(validated_changes[0].changed_paths))
            )
        else:
            assert "empty.txt" in diff and "nonempty.txt" in diff
        return

    assert record.failure_kind == "UNSUPPORTED_CHANGE"
    assert len(driver.invocations) == 1
    assert _git(repo, "count-objects", "-v").stdout == objects_before
    commands = validation_runners[0].history
    assert not any("add" in command and "-A" in command for command in commands)
    assert not any("--numstat" in command or "--full-index" in command for command in commands)
    assert not (handle.path / "git/initial.diff").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        "not-git",
        "bare",
        "reserved-file",
        "reserved-dir",
        "tracked-reserved",
        pytest.param(
            "reserved-symlink",
            marks=pytest.mark.skipif(os.name == "nt", reason="unprivileged Win32 symlinks vary"),
        ),
        pytest.param(
            "reserved-junction",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows junction contract"),
        ),
    ],
)
async def test_code_046_unsupported_repository_and_reserved_path_collisions(
    tmp_path: Path, config_data: dict[str, object], variant: str
) -> None:
    if variant == "not-git":
        repo = tmp_path / "plain"
        repo.mkdir()
    elif variant == "bare":
        repo = tmp_path / "bare.git"
        repo.mkdir()
        _git(repo, "init", "--bare")
    else:
        repo = _make_repo(tmp_path)
        reserved = repo / ".dialectic-turn"
        outside = tmp_path / "reserved-outside"
        outside.mkdir()
        (outside / "sentinel.txt").write_text("preserve me\n", encoding="utf-8")
        if variant in {"reserved-file", "tracked-reserved"}:
            reserved.write_text("user data", encoding="utf-8")
            if variant == "tracked-reserved":
                _git(repo, "add", ".dialectic-turn")
                _git(repo, "commit", "-m", "reserved collision")
        elif variant == "reserved-dir":
            reserved.mkdir()
            (reserved / "user.txt").write_text("user data", encoding="utf-8")
        elif variant == "reserved-symlink":
            os.symlink(outside, reserved, target_is_directory=True)
        else:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(reserved), str(outside)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    record, _, _ = await _execute(tmp_path, _config(config_data), repo, ScriptedAgentAdapter(target, []))
    assert record.failure_kind == "UNSUPPORTED_REPOSITORY"
    if "reserved" in variant:
        assert (repo / ".dialectic-turn").exists()
        assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "preserve me\n"
    source = Path(__file__).read_text(encoding="utf-8")
    ids = {
        int(match.group(1))
        for match in __import__("re").finditer(r"def test_code_(\d{3})_", source)
    }
    assert ids == set(range(1, 47))
