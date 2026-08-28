from __future__ import annotations

import asyncio
import copy
import ctypes
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from typer.testing import CliRunner

import dialectic.ingress as ingress_module
from dialectic.adapters import (
    AgentRegistry,
    ModelMismatchError,
    ScriptedAgentAdapter,
    ScriptedStep,
    verify_model_equivalence,
)
from dialectic.capabilities import (
    BindingBarrier,
    CapabilityEvidenceError,
    CapabilityFixture,
    build_capability_binding,
    validate_cached_attestation,
    validate_binding_identities,
    validate_or_probe_attestation,
)
from dialectic.cli import create_app
from dialectic.config import ConfigError, ConfigLoader
from dialectic.contracts import (
    ARTIFACT_SCHEMA_VERSION,
    CODE_PHASES,
    COUNCIL_PHASES,
    FAILURE_EXIT_CODES,
    FAILURE_KINDS,
    TOOL_VERSION,
    TRUNCATION_MARKER,
    exit_code_for,
)
from dialectic.ingress import InputAcquisitionError, acquire_named_file
from dialectic.launcher import LaunchPlanError, WindowsBatchLaunchSpec, build_launch_spec
from dialectic.locking import (
    RepositoryBusyError,
    RepositoryLock,
    resolve_repository_identity,
)
from dialectic.output import OutputError, extract_model_payload
from dialectic.process import (
    CREATE_SUSPENDED,
    CREATE_UNICODE_ENVIRONMENT,
    EXTENDED_STARTUPINFO_PRESENT,
    FakeProcessUnit,
    ProcessSupervisor,
    ReaderHandoffCoordinator,
    WindowsReaderHandoff,
    WindowsCreatedProcess,
    WindowsJobLauncher,
    join_reader_threads,
)
from dialectic.redaction import (
    BoundedStreamCapture,
    CredentialBoundaryError,
    KnownCredential,
    KnownCredentials,
    redact_config,
)
from dialectic.schemas import (
    AgentRequest,
    AgentRequestArtifact,
    AgentResponse,
    AgentTarget,
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    CapabilityProbeResult,
    DialecticConfig,
    RunRecord,
    TargetPreflightArtifact,
)
from dialectic.scratch import (
    ScratchCleanupTimeout,
    ScratchLimits,
    cleanup_reserved_tree,
    scan_scratch,
)
from dialectic.service import DialecticService
from dialectic.store import (
    BootstrapError,
    RunNotFoundError,
    RunStore,
    StateCorruptError,
    canonical_json_bytes,
)


def controller_fields() -> dict[str, object]:
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
    }


def make_response(*, actual_model: str | None = None) -> AgentResponse:
    return AgentResponse(
        **controller_fields(),
        runtime="codex",
        requested_model="codex-latest",
        resolved_requested_model="codex-5",
        actual_model=actual_model,
        session_id="session-1",
        text="ok",
        structured_output=None,
        usage=None,
    )


def test_core_001_valid_configuration_loads(
    config_bytes: bytes, config_data: dict[str, object]
) -> None:
    loaded = ConfigLoader().load(config_bytes, mode="code")
    assert loaded.config.model_dump() == DialecticConfig.model_validate(config_data).model_dump()
    driver, reviewers = AgentRegistry.code_targets(loaded.config)
    assert driver.runtime == "codex"
    assert reviewers[0][1] == driver


def test_core_002_unknown_credential_field_is_rejected(
    config_data: dict[str, object]
) -> None:
    config_data["driver"]["api_key"] = "not-an-accepted-field"  # type: ignore[index]
    with pytest.raises(ConfigError, match="api_key"):
        ConfigLoader().load(yaml.safe_dump(config_data).encode())


@pytest.mark.parametrize("field", ["code_review_cycles", "council_discussion_rounds"])
def test_core_003_iteration_counts_are_exactly_one(
    config_data: dict[str, object], field: str
) -> None:
    config_data["limits"][field] = 2  # type: ignore[index]
    with pytest.raises(ConfigError, match=field):
        ConfigLoader().load(yaml.safe_dump(config_data).encode())


def test_core_004_expansion_mode_sections_and_redacted_paths(
    config_data: dict[str, object]
) -> None:
    config_data["driver"]["model"] = "${DRIVER_MODEL}"  # type: ignore[index]
    raw = yaml.safe_dump(config_data).encode()
    loaded = ConfigLoader({"DRIVER_MODEL": "MODELSECRET"}).load(raw, mode="code")
    assert loaded.config.driver is not None
    assert loaded.config.driver.model == "MODELSECRET"
    artifact = redact_config(
        loaded.config,
        source_sha256=loaded.source_sha256,
        credentials=KnownCredentials([KnownCredential("TOKEN", "MODELSECRET")]),
    )
    assert artifact.redacted_field_paths == ["/driver/model"]
    assert artifact.normalized_config.driver is not None
    assert artifact.normalized_config.driver.model == "redacted"
    DialecticConfig.model_validate(artifact.normalized_config.model_dump())

    code_only = copy.deepcopy(config_data)
    code_only.pop("council")
    ConfigLoader({"DRIVER_MODEL": "codex-model"}).load(
        yaml.safe_dump(code_only).encode(), mode="code"
    )
    council_only = copy.deepcopy(config_data)
    council_only.pop("driver")
    council_only.pop("reviewers")
    ConfigLoader({"DRIVER_MODEL": "unused"}).load(
        yaml.safe_dump(council_only).encode(), mode="council"
    )
    with pytest.raises(ConfigError, match="driver is required"):
        ConfigLoader().load(yaml.safe_dump(council_only).encode(), mode="code")


def test_core_005_atomic_state_interruption_preserves_previous_record(store_factory) -> None:
    store = store_factory()
    handle = store.bootstrap_run("code")
    previous = store.read_handle(handle)
    raw_previous = (handle.path / "run.json").read_bytes()
    assert raw_previous.endswith(b"\n") and not raw_previous.endswith(b"\r\n")
    now = datetime.now(UTC)
    updated = RunRecord.model_validate(
        previous.model_copy(
            update={"status": "RUNNING", "phase": "PREFLIGHT", "updated_at": now}
        ).model_dump()
    )

    def interrupt(_source: object, _destination: object) -> None:
        raise OSError("simulated rename interruption")

    with pytest.raises(OSError, match="interruption"):
        store.write_run(handle, updated, replace_func=interrupt)
    assert store.read_handle(handle) == previous


def test_core_006_known_value_redaction_and_short_credential_preflight(
    config_data: dict[str, object]
) -> None:
    config_data["driver"]["model"] = "secretvalue"  # type: ignore[index]
    config_data["reviewers"][0]["lens"] = "secret is an ordinary short word"  # type: ignore[index]
    loaded = ConfigLoader().load(yaml.safe_dump(config_data).encode())
    credentials = KnownCredentials([KnownCredential("CODEX_TOKEN", "secretvalue")])
    artifact = redact_config(
        loaded.config,
        source_sha256=loaded.source_sha256,
        credentials=credentials,
    )
    persisted = canonical_json_bytes(artifact)
    assert b"secretvalue" not in persisted
    assert b"ordinary short word" in persisted
    with pytest.raises(CredentialBoundaryError, match="CODEX_TOKEN"):
        KnownCredentials([KnownCredential("CODEX_TOKEN", "short")])


@pytest.mark.asyncio
async def test_core_007_timeout_reaps_owned_descendant_before_sentinel() -> None:
    sentinel: list[str] = []
    unit = FakeProcessUnit(
        root_delay=2,
        sentinel_delay=0.2,
        sentinel=lambda: sentinel.append("escaped"),
    )
    result = await ProcessSupervisor().supervise(
        unit,
        turn_timeout_seconds=0.02,
        graceful_kill_seconds=0.02,
    )
    await asyncio.sleep(0.22)
    assert result.termination_reason == "timeout"
    assert result.cleanup_confirmed
    assert unit.forced
    assert sentinel == []


@pytest.mark.asyncio
async def test_core_008_cancellation_reaps_concurrent_units() -> None:
    sentinel: list[str] = []
    units = [
        FakeProcessUnit(
            root_delay=2,
            sentinel_delay=0.3,
            sentinel=lambda: sentinel.append("escaped"),
        )
        for _ in range(3)
    ]
    cancellation = asyncio.Event()
    task = asyncio.create_task(
        ProcessSupervisor().supervise_many(
            units,
            turn_timeout_seconds=2,
            graceful_kill_seconds=0.02,
            cancellation=cancellation,
        )
    )
    await asyncio.sleep(0.02)
    cancellation.set()
    results = await task
    assert {result.termination_reason for result in results} == {"cancelled"}
    assert all(unit.forced for unit in units)
    assert sentinel == []


@pytest.mark.asyncio
async def test_core_009_parallel_intervals_overlap() -> None:
    target = AgentTarget(runtime="codex", model="codex-latest", effort=None)
    adapters = [
        ScriptedAgentAdapter(target, [ScriptedStep(make_response(), delay_seconds=0.05)])
        for _ in range(2)
    ]
    request = AgentRequest(
        role="reviewer",
        target_id="reviewer-a",
        turn_phase="review",
        prompt="review this",
        output_schema=None,
        timeout_seconds=1,
        working_directory=".",
        access_mode="packet-only",
    )
    await asyncio.gather(*(adapter.start(request) for adapter in adapters))
    intervals = [adapter.invocations[0] for adapter in adapters]
    assert max(item.started_at for item in intervals) < min(
        item.completed_at for item in intervals
    )


def test_core_010_model_alias_and_mismatch_contract() -> None:
    aliases = {"latest": "codex-5", "codex-5-alias": "codex-5"}
    verify_model_equivalence(
        requested="latest",
        resolved="codex-5",
        actual="codex-5-alias",
        aliases=aliases,
    )
    with pytest.raises(ModelMismatchError):
        verify_model_equivalence(
            requested="latest",
            resolved="codex-5",
            actual="different-model",
            aliases=aliases,
        )


@pytest.mark.integration
def test_core_011_dial_and_dialectic_are_equivalent(
    tmp_path: Path, config_bytes: bytes
) -> None:
    config_path = tmp_path / "dialectic.yaml"
    task_path = tmp_path / "task.md"
    config_path.write_bytes(config_bytes)
    task_path.write_text("task", encoding="utf-8")
    oversized_path = tmp_path / "oversized-task.md"
    oversized_path.write_bytes(b"x" * 262_145)
    runner = CliRunner()
    cases = (task_path, tmp_path / "missing-task.md", oversized_path)
    for case_index, selected_task in enumerate(cases, start=1):
        run_id = f"20260828T01010{case_index}Z-aaaaaaaaaa"
        outcomes: list[tuple[int, list[str], str, str | None]] = []
        for executable_index, executable_name in enumerate(("dial", "dialectic")):
            state = tmp_path / f"state-{case_index}-{executable_index}"
            store = RunStore(state, run_id_factory=lambda value=run_id: value)
            service = DialecticService(store)
            app = create_app(lambda service=service: service)
            result = runner.invoke(
                app,
                [
                    "code",
                    "--config",
                    str(config_path),
                    "--repo",
                    str(tmp_path),
                    "--task-file",
                    str(selected_task),
                ],
                prog_name=executable_name,
            )
            record = store.read_run(run_id)
            tree = sorted(
                str(path.relative_to(state / "runs" / record.run_id))
                for path in (state / "runs" / record.run_id).rglob("*")
            )
            outcomes.append((result.exit_code, tree, record.status, record.failure_kind))
        assert outcomes[0] == outcomes[1]

    scripts_directory = Path(sys.executable).parent
    suffix = ".exe" if os.name == "nt" else ""
    help_results = [
        subprocess.run(
            [str(scripts_directory / f"{name}{suffix}"), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        for name in ("dial", "dialectic")
    ]
    assert [result.returncode for result in help_results] == [0, 0]
    for result in help_results:
        assert all(command in result.stdout for command in ("code", "council", "status"))


def test_core_012_windows_batch_shim_is_constrained(tmp_path: Path) -> None:
    safe = build_launch_spec(
        tmp_path / "vendor.cmd",
        ["--model", "codex-5"],
        windows=True,
        system_directory=tmp_path / "System32",
    )
    assert isinstance(safe, WindowsBatchLaunchSpec)
    assert safe.spawned_root_executable.name == "cmd.exe"
    assert safe.root_arguments[:5] == ("/d", "/q", "/v:off", "/s", "/c")
    with pytest.raises(LaunchPlanError):
        build_launch_spec(
            tmp_path / "bad&shim.cmd",
            ["--model", "codex-5"],
            windows=True,
            system_directory=tmp_path,
        )
    with pytest.raises(LaunchPlanError):
        build_launch_spec(
            tmp_path / "vendor.cmd",
            ["unsafe&argument"],
            windows=True,
            system_directory=tmp_path,
        )


def test_core_013_large_prompt_uses_request_payload_not_argv(tmp_path: Path) -> None:
    prompt = ("$() `%PATH% ; & | <>\n" * 10_000)[: 200 * 1024]
    request = AgentRequest(
        role="driver",
        target_id="driver",
        turn_phase="initial",
        prompt=prompt,
        output_schema=None,
        timeout_seconds=300,
        working_directory=str(tmp_path),
        access_mode="driver-write",
    )
    spec = build_launch_spec(tmp_path / "codex.exe", ["exec", "-"], windows=True)
    assert request.prompt.encode("utf-8") == prompt.encode("utf-8")
    assert all(len(argument.encode("utf-8")) <= 4096 for argument in spec.arguments)
    assert prompt not in spec.arguments


@pytest.mark.parametrize("run_id", ["../run", "20260828T010101Z-AAAAAAAAAA", "x", ""])
def test_core_014_run_id_is_validated_before_path_join(tmp_path: Path, run_id: str) -> None:
    class PathJoinForbidden:
        def __truediv__(self, part: object) -> Path:
            raise AssertionError(f"path joining occurred for invalid run id: {part!r}")

    store = RunStore(tmp_path / "state")
    store.runs_root = PathJoinForbidden()  # type: ignore[assignment]
    result = CliRunner().invoke(
        create_app(lambda: DialecticService(store)),
        ["status", run_id],
    )
    assert result.exit_code == 2
    assert "canonical grammar" in result.output


def test_core_015_controller_artifact_schemas_are_closed_and_versioned() -> None:
    base = {
        "role": "driver",
        "target_id": "driver",
        "turn_phase": "initial",
        "outbound_prompt_sha256": "a" * 64,
        "persisted_prompt_sha256": "b" * 64,
        "prompt": "task",
        "output_schema": None,
        "timeout_seconds": 30,
        "access_mode": "driver-write",
        "tool_version": TOOL_VERSION,
    }
    with pytest.raises(ValidationError):
        AgentRequestArtifact.model_validate(base)
    with pytest.raises(ValidationError):
        AgentRequestArtifact.model_validate(
            {**base, "artifact_schema_version": 2, "unknown": True}
        )
    with pytest.raises(ValidationError):
        CapabilityProbeResult(
            probe_id="network-denied",
            expected="deny",
            observed="unavailable",
            passed=True,
            bounded_diagnostic=None,
        )
    with pytest.raises(ValidationError):
        CapabilityBindingArtifact(
            **controller_fields(),
            binding_id="binding",
            role="driver",
            target_id="driver",
            access_mode="driver-write",
            target_preflight_artifact_sha256="a" * 64,
            capability_attestation_sha256="b" * 64,
            profile_template_sha256="c" * 64,
            concrete_profile_sha256="d" * 64,
            dynamic_filesystem_identities=[],
            canonical_instantiation_verified=False,
        )


def test_core_016_configuration_bounds_name_the_invalid_field(
    config_data: dict[str, object]
) -> None:
    mutations = [
        (lambda value: value["limits"].__setitem__("agent_turn_seconds", 0), "agent_turn_seconds"),
        (lambda value: value["reviewers"][0].__setitem__("id", "Bad_ID"), "reviewers.0.id"),
        (lambda value: value["driver"].__setitem__("model", "bad model"), "driver.model"),
        (lambda value: value["reviewers"][0].__setitem__("lens", "x" * 5000), "lens"),
    ]
    for mutate, expected in mutations:
        invalid = copy.deepcopy(config_data)
        mutate(invalid)
        with pytest.raises(ConfigError, match=expected):
            ConfigLoader().load(yaml.safe_dump(invalid).encode())


def test_core_017_secure_bootstrap_failure_and_collision_bound(tmp_path: Path) -> None:
    state = tmp_path / "privacy-state"

    def reject_privacy(_path: Path, _parent: Path) -> None:
        raise PermissionError("privacy unavailable")

    store = RunStore(
        state,
        run_id_factory=lambda: "20260828T020202Z-aaaaaaaaaa",
        privacy_verifier=reject_privacy,
    )
    with pytest.raises(BootstrapError):
        store.bootstrap_run("code")
    assert not (state / "runs" / "20260828T020202Z-aaaaaaaaaa").exists()

    candidates = [
        "20260828T020203Z-aaaaaaaaaa",
        "20260828T020204Z-aaaaaaaaaa",
        "20260828T020205Z-aaaaaaaaaa",
    ]
    attempts = 0

    def next_id() -> str:
        nonlocal attempts
        value = candidates[attempts]
        attempts += 1
        return value

    collision_store = RunStore(
        tmp_path / "collision-state",
        run_id_factory=next_id,
        bootstrap_suffix_factory=lambda: "abcdefabcdef",
    )
    for candidate in candidates:
        final = collision_store.runs_root / candidate
        final.mkdir()
        (final / "sentinel").write_text("untouched", encoding="utf-8")
    with pytest.raises(BootstrapError):
        collision_store.bootstrap_run("code")
    assert attempts == 3
    assert all(
        (collision_store.runs_root / candidate / "sentinel").read_text() == "untouched"
        for candidate in candidates
    )
    assert not list(collision_store.runs_root.glob(".*.bootstrap-*"))

    identity = resolve_repository_identity(tmp_path)
    first = RepositoryLock(collision_store.state_root / "locks", identity, "run-a").acquire()
    second = RepositoryLock(collision_store.state_root / "locks", identity, "run-b")
    with pytest.raises(RepositoryBusyError) as busy:
        second.acquire()
    assert busy.value.holding_run_id == "run-a"
    first.release()
    second.acquire()
    second.release()


def test_core_018_strict_output_extraction_accepts_only_two_forms() -> None:
    class Payload(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        value: int

    whole = extract_model_payload(
        '{"value":1}', Payload, max_chars=100, max_items=10
    )
    fenced = extract_model_payload(
        'result:\n```json\n{"value":2}\n```',
        Payload,
        max_chars=100,
        max_items=10,
    )
    assert (whole.value, fenced.value) == (1, 2)
    invalid = [
        "prose only",
        '```json\n{"value":1}\n```\n```json\n{"value":2}\n```',
        '{"value":1,"value":2}',
        '{"value":NaN}',
        '{"value":"1"}',
        '"\\ud800"',
        "[" * 65 + "]" * 65,
    ]
    for text in invalid:
        with pytest.raises(OutputError):
            extract_model_payload(text, Payload, max_chars=100, max_items=10)


@pytest.mark.asyncio
async def test_core_019_unconfirmed_tree_cleanup_overrides_timeout() -> None:
    unit = FakeProcessUnit(root_delay=1, cleanup_confirmed=False)
    result = await ProcessSupervisor().supervise(
        unit,
        turn_timeout_seconds=0.01,
        graceful_kill_seconds=0.01,
    )
    assert result.termination_reason == "cleanup-failed"
    assert result.failure_kind == "PROCESS_CLEANUP_FAILED"


def test_core_020_absent_actual_model_is_recorded_without_mismatch() -> None:
    response = make_response(actual_model=None)
    assert response.actual_model is None
    verify_model_equivalence(
        requested="codex-latest",
        resolved="codex-5",
        actual=None,
        aliases={},
    )


def test_core_021_well_formed_unknown_status_is_exit_two(tmp_path: Path) -> None:
    service = DialecticService(RunStore(tmp_path / "state"))
    result = CliRunner().invoke(
        create_app(lambda: service),
        ["status", "20260828T030303Z-aaaaaaaaaa"],
    )
    assert result.exit_code == 2
    with pytest.raises(RunNotFoundError):
        service.get_run("20260828T030303Z-aaaaaaaaaa")


def test_core_022_status_displays_running_finalized_and_failed(tmp_path: Path) -> None:
    runner = CliRunner()
    observed: list[str] = []
    for index, terminal in enumerate(("RUNNING", "FINALIZED", "FAILED"), start=1):
        run_id = f"20260828T0303{index:02d}Z-aaaaaaaaaa"
        store = RunStore(tmp_path / f"state-{index}", run_id_factory=lambda value=run_id: value)
        service = DialecticService(store)
        handle = service.create_run("code")
        if terminal == "RUNNING":
            service.start_run(handle, phase="PREFLIGHT")
        elif terminal == "FINALIZED":
            service.start_run(handle, phase="PREFLIGHT")
            service.finalize_code(handle, "COMPLETED_NO_FINDINGS")
        else:
            service.fail_run(handle, "PREFLIGHT_FAILED", "expected")
        result = runner.invoke(create_app(lambda service=service: service), ["status", run_id])
        assert result.exit_code == 0
        assert str(service.run_artifact_directory(run_id)) in result.output
        observed.append(service.get_run(run_id).status)
    assert observed == ["RUNNING", "FINALIZED", "FAILED"]


@pytest.mark.parametrize("corrupt", [b"{", b"not-json", b'{"artifact_schema_version":1}\n'])
def test_core_023_corrupt_run_state_is_not_guessed(tmp_path: Path, corrupt: bytes) -> None:
    run_id = "20260828T040404Z-aaaaaaaaaa"
    store = RunStore(tmp_path / "state", run_id_factory=lambda: run_id)
    handle = store.bootstrap_run("code")
    (handle.path / "run.json").write_bytes(corrupt)
    service = DialecticService(store)
    result = CliRunner().invoke(create_app(lambda: service), ["status", run_id])
    assert result.exit_code == 3
    with pytest.raises(StateCorruptError):
        service.get_run(run_id)


def test_core_024_yaml_and_environment_grammar_is_closed(
    config_data: dict[str, object]
) -> None:
    valid = yaml.safe_dump(config_data, sort_keys=False)
    invalid_documents = [
        "tagged: !!python/object:builtins.object {}\n" + valid,
        "anchor: &x value\nalias: *x\n" + valid,
        "version: 1\nversion: 1\n" + "\n".join(valid.splitlines()[1:]),
    ]
    for document in invalid_documents:
        with pytest.raises(ConfigError):
            ConfigLoader().load(document.encode())

    partial = copy.deepcopy(config_data)
    partial["driver"]["model"] = "prefix-${MODEL}"  # type: ignore[index]
    with pytest.raises(ConfigError, match="partial"):
        ConfigLoader({"MODEL": "codex"}).load(yaml.safe_dump(partial).encode())
    missing = copy.deepcopy(config_data)
    missing["driver"]["model"] = "${MISSING}"  # type: ignore[index]
    with pytest.raises(ConfigError, match="missing or empty"):
        ConfigLoader({}).load(yaml.safe_dump(missing).encode())
    with pytest.raises(ConfigError, match="missing or empty"):
        ConfigLoader({"MISSING": ""}).load(yaml.safe_dump(missing).encode())
    escaped = copy.deepcopy(config_data)
    escaped["reviewers"][0]["lens"] = "$${MODEL} costs $5"  # type: ignore[index]
    loaded = ConfigLoader({}).load(yaml.safe_dump(escaped).encode())
    assert loaded.config.reviewers is not None
    assert loaded.config.reviewers[0].lens == "${MODEL} costs $5"


def test_core_025_status_phase_and_explicit_null_contracts() -> None:
    now = datetime.now(UTC)

    def record(mode: str, status: str, phase: str | None, **updates: object) -> RunRecord:
        data: dict[str, object] = {
            **controller_fields(),
            "run_id": "20260828T050505Z-aaaaaaaaaa",
            "mode": mode,
            "status": status,
            "phase": phase,
            "code_outcome": None,
            "consensus_outcome": None,
            "failure_kind": None,
            "failure_detail": None,
            "created_at": now,
            "updated_at": now,
            "started_model_work_at": None,
            "completed_at": None,
        }
        data.update(updates)
        return RunRecord.model_validate(data)

    created = record("code", "CREATED", None)
    assert set(created.model_dump()) == set(RunRecord.model_fields)
    for mode, phases in (("code", CODE_PHASES), ("council", COUNCIL_PHASES)):
        for phase in phases:
            assert record(mode, "RUNNING", phase).phase == phase
    record(
        "code",
        "FINALIZED",
        "REPORTING",
        code_outcome="COMPLETED_NO_FINDINGS",
        completed_at=now,
    )
    record(
        "council",
        "FAILED",
        "PREFLIGHT",
        failure_kind="PREFLIGHT_FAILED",
        failure_detail="failure",
        completed_at=now,
    )
    record("code", "TIMED_OUT", "DRIVER_INITIAL", completed_at=now)
    record("council", "CANCELLED", "BALLOTS", completed_at=now)
    with pytest.raises(ValidationError, match="invalid for code"):
        record("code", "RUNNING", "BALLOTS")
    with pytest.raises(ValidationError, match="phase=null"):
        record("code", "FAILED", None, failure_kind="INTERNAL_ERROR", completed_at=now)


def test_core_026_failure_and_bounded_ingress_contracts(
    tmp_path: Path, config_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index, kind in enumerate(FAILURE_KINDS, start=1):
        run_id = f"20260828T0606{index:02d}Z-aaaaaaaaaa"
        service = DialecticService(
            RunStore(tmp_path / f"state-{index}", run_id_factory=lambda value=run_id: value)
        )
        record = service.fail_run(service.create_run("code"), kind, f"trigger {kind}")
        assert record.status == "FAILED"
        assert exit_code_for(record.status, record.failure_kind) == FAILURE_EXIT_CODES[kind]
    assert exit_code_for("TIMED_OUT") == 4
    assert exit_code_for("CANCELLED") == 130

    missing = tmp_path / "missing"
    with pytest.raises(InputAcquisitionError):
        acquire_named_file(missing, label="input")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(InputAcquisitionError, match="regular"):
        acquire_named_file(directory, label="input")
    sparse = tmp_path / "sparse"
    with sparse.open("wb") as stream:
        stream.seek(262_144)
        stream.write(b"x")
    assert sparse.stat().st_size == 262_145
    with pytest.raises(InputAcquisitionError, match="exceeds"):
        acquire_named_file(sparse, label="input")

    ceiling = 131_073
    bounded = tmp_path / "bounded"
    expected = b"x" * ceiling
    bounded.write_bytes(expected)
    selected_read_calls: list[int] = []
    if os.name == "nt":
        selected_reader = ingress_module._read_windows_handle

        def track_selected_read(kernel32: object, handle: int, count: int) -> bytes:
            selected_read_calls.append(count)
            return selected_reader(kernel32, handle, count)

        monkeypatch.setattr(ingress_module, "_read_windows_handle", track_selected_read)
    else:
        selected_reader = ingress_module._read_fd_bounded

        def track_selected_read(fd: int, count: int) -> bytes:
            selected_read_calls.append(count)
            return selected_reader(fd, count)

        monkeypatch.setattr(ingress_module, "_read_fd_bounded", track_selected_read)
    assert acquire_named_file(bounded, label="input", ceiling=ceiling) == expected
    assert selected_read_calls == [ceiling + 1]

    read_payload = bytearray(b"p" * (ceiling + 1))
    posix_read_sizes: list[int] = []

    def fake_os_read(fd: int, count: int) -> bytes:
        posix_read_sizes.append(count)
        chunk = bytes(read_payload[:count])
        del read_payload[:count]
        return chunk

    with monkeypatch.context() as patch:
        patch.setattr(ingress_module.os, "read", fake_os_read)
        assert len(ingress_module._read_fd_bounded(1, ceiling + 1)) == ceiling + 1
    assert sum(posix_read_sizes) == ceiling + 1
    assert max(posix_read_sizes) <= 65_536

    class FakeKernel32:
        def __init__(self) -> None:
            self.payload = bytearray(b"w" * (ceiling + 1))
            self.read_sizes: list[int] = []

        def ReadFile(
            self,
            handle: object,
            buffer: object,
            requested: int,
            read_pointer: object,
            overlapped: object,
        ) -> int:
            self.read_sizes.append(requested)
            chunk = bytes(self.payload[:requested])
            del self.payload[:requested]
            ctypes.memmove(buffer, chunk, len(chunk))
            read_pointer._obj.value = len(chunk)  # type: ignore[attr-defined]
            return 1

    fake_kernel32 = FakeKernel32()
    assert len(
        ingress_module._read_windows_handle(fake_kernel32, object(), ceiling + 1)
    ) == ceiling + 1
    assert sum(fake_kernel32.read_sizes) == ceiling + 1
    assert max(fake_kernel32.read_sizes) <= 65_536

    growing = tmp_path / "growing"
    growing.write_bytes(b"before")
    if os.name == "nt":
        monkeypatch.setattr(ingress_module, "_read_windows_handle", selected_reader)

        def grow_during_windows_read(kernel32: object, handle: int, count: int) -> bytes:
            data = selected_reader(kernel32, handle, count)
            with growing.open("ab") as stream:
                stream.write(b"after")
            return data

        monkeypatch.setattr(
            ingress_module, "_read_windows_handle", grow_during_windows_read
        )
    else:
        monkeypatch.setattr(ingress_module, "_read_fd_bounded", selected_reader)

        def grow_during_posix_read(fd: int, count: int) -> bytes:
            data = selected_reader(fd, count)
            with growing.open("ab") as stream:
                stream.write(b"after")
            return data

        monkeypatch.setattr(ingress_module, "_read_fd_bounded", grow_during_posix_read)
    with pytest.raises(InputAcquisitionError, match="changed"):
        acquire_named_file(growing, label="input")

    with pytest.raises(InputAcquisitionError, match="regular|device"):
        acquire_named_file(os.devnull, label="input")
    if os.name == "nt":
        for special in ("NUL", r"\\.\pipe\dialectic-core-026"):
            with pytest.raises(InputAcquisitionError, match="device"):
                acquire_named_file(special, label="input")
    else:
        fifo = tmp_path / "input.fifo"
        os.mkfifo(fifo)
        with pytest.raises(InputAcquisitionError, match="regular"):
            acquire_named_file(fifo, label="input")
        socket_path = tmp_path / "input.socket"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unix_socket:
            unix_socket.bind(str(socket_path))
            with pytest.raises(InputAcquisitionError, match="regular"):
                acquire_named_file(socket_path, label="input")

    for offset, invalid_config, invalid_task in (
        (30, config_bytes, b"\xff"),
        (31, b"version: 1\nunexpected: true\n", b"task"),
    ):
        service = DialecticService(
            RunStore(
                tmp_path / f"invalid-state-{offset}",
                run_id_factory=lambda value=offset: f"20260828T0606{value:02d}Z-aaaaaaaaaa",
            )
        )
        record = asyncio.run(
            service.execute_code_once(
                service.create_run("code"),
                config_bytes=invalid_config,
                task_bytes=invalid_task,
                repository_path=tmp_path,
            )
        )
        assert (record.status, record.failure_kind) == ("FAILED", "INVALID_INPUT")

    class TrackingService(DialecticService):
        invalid_input_calls = 0

        def fail_invalid_input(self, handle, diagnostic):  # type: ignore[no-untyped-def]
            self.invalid_input_calls += 1
            return super().fail_invalid_input(handle, diagnostic)

    cli_run_id = "20260828T060632Z-aaaaaaaaaa"
    tracking_service = TrackingService(
        RunStore(
            tmp_path / "cli-invalid-state",
            run_id_factory=lambda: cli_run_id,
        )
    )
    config_path = tmp_path / "valid-config.yaml"
    config_path.write_bytes(config_bytes)
    cli_result = CliRunner().invoke(
        create_app(lambda: tracking_service),
        [
            "code",
            "--config",
            str(config_path),
            "--repo",
            str(tmp_path),
            "--task-file",
            str(tmp_path / "missing-task"),
        ],
    )
    assert cli_result.exit_code == 2
    assert tracking_service.invalid_input_calls == 1
    assert tracking_service.get_run(cli_run_id).failure_kind == "INVALID_INPUT"


def test_core_027_stream_scratch_and_cleanup_bounds(tmp_path: Path) -> None:
    secret = "credential-value"
    credentials = KnownCredentials([KnownCredential("TOKEN", secret)])
    capture = BoundedStreamCapture(256, credentials)
    prefix = b"x" * 230 + secret[:5].encode()
    capture.feed(prefix)
    assert capture.feed(secret[5:].encode() + b"overflow" * 10)
    finished = capture.finish()
    assert finished.result.truncated
    assert finished.persisted.endswith(TRUNCATION_MARKER)
    assert secret.encode() not in finished.persisted
    assert len(finished.persisted) <= 256
    assert hashlib.sha256(finished.persisted).hexdigest() == finished.result.persisted_sha256

    with pytest.raises(CredentialBoundaryError, match="max_agent_stdout_bytes"):
        credentials.validate_stream_limits(stdout_bytes=90, stderr_bytes=256)

    bytes_root = tmp_path / "scratch-bytes"
    bytes_root.mkdir()
    (bytes_root / "large").write_bytes(b"x" * 20)
    usage = scan_scratch(bytes_root, ScratchLimits(10, 10, 3))
    assert usage.overage == "bytes"
    assert usage.logical_regular_file_bytes == 11

    entries_root = tmp_path / "scratch-entries"
    entries_root.mkdir()
    for index in range(3):
        (entries_root / str(index)).touch()
    assert scan_scratch(entries_root, ScratchLimits(100, 2, 3)).entry_count == 3

    depth_root = tmp_path / "scratch-depth"
    (depth_root / "one" / "two").mkdir(parents=True)
    assert scan_scratch(depth_root, ScratchLimits(100, 10, 1)).maximum_depth == 2

    cleanup_root = tmp_path / "cleanup"
    cleanup_root.mkdir()
    (cleanup_root / "entry").touch()
    ticks = iter((0.0, 1.0, 2.0))
    with pytest.raises(ScratchCleanupTimeout):
        cleanup_reserved_tree(
            cleanup_root,
            timeout_seconds=0.1,
            clock=lambda: next(ticks),
        )


@pytest.mark.asyncio
async def test_core_028_windows_reader_handoff_is_byte_and_callback_bounded() -> None:
    class ChunkedPipe:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self._lock = threading.Lock()
            self.read_count = 0
            self.maximum_requested = 0

        def read(self, size: int) -> bytes:
            with self._lock:
                self.read_count += 1
                self.maximum_requested = max(self.maximum_requested, size)
                if not self._chunks:
                    return b""
                chunk = self._chunks.pop(0)
            assert len(chunk) <= size
            return chunk

    def wait_until(predicate, timeout: float = 1.0) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for reader state")
            time.sleep(0.001)

    loop = asyncio.get_running_loop()
    stalled_coordinator = ReaderHandoffCoordinator()
    stalled_notifications: list[str] = []
    stdout = WindowsReaderHandoff(
        limit_bytes=262_144,
        queue_capacity_bytes=65_536,
        coordinator=stalled_coordinator,
        notify=lambda: stalled_notifications.append("stdout"),
        loop=loop,
    )
    stderr = WindowsReaderHandoff(
        limit_bytes=262_144,
        queue_capacity_bytes=65_536,
        coordinator=stalled_coordinator,
        notify=lambda: stalled_notifications.append("stderr"),
        loop=loop,
    )
    stdout_pipe = ChunkedPipe([b"o" * 16_384 for _ in range(16)])
    stderr_pipe = ChunkedPipe([b"e" * 16_384 for _ in range(16)])
    stalled_threads = [
        threading.Thread(target=stdout.read_pipe, args=(stdout_pipe,), daemon=True),
        threading.Thread(target=stderr.read_pipe, args=(stderr_pipe,), daemon=True),
    ]
    for reader in stalled_threads:
        reader.start()
    time.sleep(0.05)  # Deliberately prevent the scheduled loop notifications from running.
    assert stalled_notifications == []
    assert stdout.notification_count == stderr.notification_count == 1
    assert all(reader.is_alive() for reader in stalled_threads)
    assert stdout.peak_queued_bytes <= stdout.queue_capacity_bytes
    assert stderr.peak_queued_bytes <= stderr.queue_capacity_bytes
    assert stdout.peak_resident_bytes <= stdout.queue_capacity_bytes + 65_536
    assert stderr.peak_resident_bytes <= stderr.queue_capacity_bytes + 65_536
    while any(reader.is_alive() for reader in stalled_threads):
        stdout.drain()
        stderr.drain()
        await asyncio.sleep(0)
    assert await join_reader_threads(
        stalled_threads, coordinator=stalled_coordinator, timeout_seconds=1
    )
    assert stdout_pipe.maximum_requested <= 65_536
    assert stderr_pipe.maximum_requested <= 65_536

    abort_coordinator = ReaderHandoffCoordinator()
    abort_handoff = WindowsReaderHandoff(
        limit_bytes=12,
        queue_capacity_bytes=4,
        coordinator=abort_coordinator,
        notify=lambda: None,
    )
    abort_pipe = ChunkedPipe([b"a" * 4, b"b" * 4])
    abort_thread = threading.Thread(
        target=abort_handoff.read_pipe, args=(abort_pipe,), daemon=True
    )
    abort_thread.start()
    wait_until(lambda: abort_pipe.read_count >= 2)
    assert abort_thread.is_alive()
    assert abort_handoff.queued_bytes == 4
    abort_coordinator.trigger_abort()
    abort_thread.join(1)
    assert not abort_thread.is_alive()
    assert not abort_coordinator.overflow.is_set()

    flood_coordinator = ReaderHandoffCoordinator()
    flood_handoff = WindowsReaderHandoff(
        limit_bytes=1_048_576,
        queue_capacity_bytes=65_536,
        coordinator=flood_coordinator,
        notify=lambda: None,
    )
    flood_pipe = ChunkedPipe([b"f" * 4_096 for _ in range(256)])
    flood_thread = threading.Thread(
        target=flood_handoff.read_pipe, args=(flood_pipe,), daemon=True
    )
    flood_thread.start()
    captured_bytes = 0
    while flood_thread.is_alive():
        captured_bytes += sum(len(chunk) for chunk in flood_handoff.drain())
        await asyncio.sleep(0)
    captured_bytes += sum(len(chunk) for chunk in flood_handoff.drain())
    assert captured_bytes == 1_048_576
    assert not flood_coordinator.overflow.is_set()
    assert flood_handoff.peak_queued_bytes <= flood_handoff.queue_capacity_bytes

    overflow_coordinator = ReaderHandoffCoordinator()
    waiting_handoff = WindowsReaderHandoff(
        limit_bytes=12,
        queue_capacity_bytes=4,
        coordinator=overflow_coordinator,
        notify=lambda: None,
    )
    overflowing_handoff = WindowsReaderHandoff(
        limit_bytes=4,
        queue_capacity_bytes=4,
        coordinator=overflow_coordinator,
        notify=lambda: None,
    )
    waiting_pipe = ChunkedPipe([b"w" * 4, b"x" * 4])
    waiting_thread = threading.Thread(
        target=waiting_handoff.read_pipe, args=(waiting_pipe,), daemon=True
    )
    waiting_thread.start()
    wait_until(lambda: waiting_pipe.read_count >= 2)
    overflow_thread = threading.Thread(
        target=overflowing_handoff.read_pipe,
        args=(ChunkedPipe([b"!" * 5]),),
        daemon=True,
    )
    overflow_thread.start()
    waiting_thread.join(1)
    overflow_thread.join(1)
    assert not waiting_thread.is_alive() and not overflow_thread.is_alive()
    assert overflow_coordinator.overflow_transition_count == 1
    assert not overflow_coordinator.trigger_overflow()
    assert overflow_coordinator.overflow_transition_count == 1

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

    class Backend:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.closed: set[int] = set()
            self.job = Resource("job")
            self.process = WindowsCreatedProcess(
                Resource("process"), Resource("thread"), 123
            )
            self.entrypoint_ran = False

        def nested_jobs_supported(self):
            self.calls.append("nested-probe")
            return True

        def create_kill_on_close_job(self):
            self.calls.append("job")
            return self.job

        def create_standard_stream_pipes(self):
            self.calls.append("pipes")
            return {
                "stdout": (Resource("stdout-parent"), Resource("stdout-child")),
                "stderr": (Resource("stderr-parent"), Resource("stderr-child")),
            }

        def create_attribute_list(self, *, job, inherited_handles):
            self.calls.append("attributes")
            assert job is self.job and len(inherited_handles) == 2
            return Resource("attributes")

        def create_process_suspended(self, **kwargs):
            self.calls.append("create-suspended")
            assert kwargs["creation_flags"] == (
                EXTENDED_STARTUPINFO_PRESENT
                | CREATE_SUSPENDED
                | CREATE_UNICODE_ENVIRONMENT
            )
            return self.process

        def verify_job_membership(self, process, job):
            self.calls.append("verify")
            return process is self.process.process_handle and job is self.job

        def resume_thread(self, thread):
            self.calls.append("resume")
            assert thread is self.process.thread_handle
            assert "verify" in self.calls
            self.entrypoint_ran = True  # Models an immediate descendant/sentinel attempt.

        def request_graceful_termination(self, process):
            self.calls.append("graceful")

        def terminate_job(self, job):
            self.calls.append("terminate-job")

        def wait_process(self, process, timeout_seconds):
            return 0

        def close_resource(self, resource):
            assert id(resource) not in self.closed
            self.closed.add(id(resource))

        def resource_is_closed(self, resource):
            return id(resource) in self.closed

    backend = Backend()
    unit = WindowsJobLauncher(backend).launch(
        executable="agent.exe",
        arguments=("--version",),
        cwd="C:\\neutral",
        environment={"SystemRoot": "C:\\Windows"},
    )
    assert backend.calls[:6] == [
        "nested-probe",
        "job",
        "pipes",
        "attributes",
        "create-suspended",
        "verify",
    ]
    assert backend.calls[6] == "resume"
    assert backend.entrypoint_ran
    job_result = await ProcessSupervisor().supervise(
        unit,
        turn_timeout_seconds=1,
        graceful_kill_seconds=1,
    )
    assert job_result.termination_reason == "completed"
    assert job_result.cleanup_confirmed
    assert "terminate-job" in backend.calls
    assert len(backend.closed) == 8

    class JobListFailureBackend(Backend):
        def create_attribute_list(self, *, job, inherited_handles):
            self.calls.append("job-list-failed")
            raise RuntimeError("creation-time Job-list assignment failed")

    job_list_backend = JobListFailureBackend()
    with pytest.raises(RuntimeError, match="Job-list"):
        WindowsJobLauncher(job_list_backend).launch(
            executable="agent.exe",
            arguments=(),
            cwd="C:\\neutral",
            environment={"SystemRoot": "C:\\Windows"},
        )
    assert "create-suspended" not in job_list_backend.calls
    assert "terminate-job" in job_list_backend.calls
    assert len(job_list_backend.closed) == 5

    class UnsupportedNestedJobBackend(Backend):
        def nested_jobs_supported(self):
            self.calls.append("nested-probe-failed")
            return False

    nested_backend = UnsupportedNestedJobBackend()
    with pytest.raises(RuntimeError, match="nested Windows Job"):
        WindowsJobLauncher(nested_backend).launch(
            executable="agent.exe",
            arguments=(),
            cwd="C:\\neutral",
            environment={"SystemRoot": "C:\\Windows"},
        )
    assert nested_backend.calls == ["nested-probe-failed"]

    class MembershipFailureBackend(Backend):
        def verify_job_membership(self, process, job):
            self.calls.append("verify-failed")
            return False

    failed_backend = MembershipFailureBackend()
    with pytest.raises(RuntimeError, match="membership"):
        WindowsJobLauncher(failed_backend).launch(
            executable="agent.exe",
            arguments=(),
            cwd="C:\\neutral",
            environment={"SystemRoot": "C:\\Windows"},
        )
    assert "resume" not in failed_backend.calls
    assert len(failed_backend.closed) == 8

    class BlockingBackend(Backend):
        def __init__(self) -> None:
            super().__init__()
            self.terminated = threading.Event()

        def wait_process(self, process, timeout_seconds):
            wait_seconds = 1 if timeout_seconds is None else timeout_seconds
            return 9 if self.terminated.wait(wait_seconds) else None

        def terminate_job(self, job):
            super().terminate_job(job)
            self.terminated.set()

    blocking_backend = BlockingBackend()
    blocking_unit = WindowsJobLauncher(blocking_backend).launch(
        executable="agent.exe",
        arguments=(),
        cwd="C:\\neutral",
        environment={"SystemRoot": "C:\\Windows"},
    )
    overflow_event = asyncio.Event()
    overflow_event.set()
    overflow_result = await ProcessSupervisor().supervise(
        blocking_unit,
        turn_timeout_seconds=1,
        graceful_kill_seconds=0.01,
        overflow=overflow_event,
    )
    assert overflow_result.termination_reason == "output-limit"
    assert overflow_result.failure_kind == "AGENT_OUTPUT_TOO_LARGE"
    assert overflow_result.cleanup_confirmed
    assert "terminate-job" in blocking_backend.calls
    assert len(blocking_backend.closed) == 8

    class UnclosedJobBackend(BlockingBackend):
        def close_resource(self, resource):
            if resource is self.job:
                return
            super().close_resource(resource)

    unclosed_backend = UnclosedJobBackend()
    unclosed_unit = WindowsJobLauncher(unclosed_backend).launch(
        executable="agent.exe",
        arguments=(),
        cwd="C:\\neutral",
        environment={"SystemRoot": "C:\\Windows"},
    )
    cleanup_overflow = asyncio.Event()
    cleanup_overflow.set()
    cleanup_result = await ProcessSupervisor().supervise(
        unclosed_unit,
        turn_timeout_seconds=1,
        graceful_kill_seconds=0.01,
        overflow=cleanup_overflow,
    )
    assert cleanup_result.termination_reason == "cleanup-failed"
    assert cleanup_result.failure_kind == "PROCESS_CLEANUP_FAILED"

    slow_threads = [
        threading.Thread(target=time.sleep, args=(0.1,), daemon=True),
        threading.Thread(target=time.sleep, args=(0.1,), daemon=True),
    ]
    for reader in slow_threads:
        reader.start()
    join_started = time.monotonic()
    assert not await join_reader_threads(
        slow_threads,
        coordinator=ReaderHandoffCoordinator(),
        timeout_seconds=0.01,
    )
    assert time.monotonic() - join_started < 0.08
    for reader in slow_threads:
        reader.join(1)


def test_core_029_only_scalar_utf8_without_bom_is_accepted(
    tmp_path: Path, config_bytes: bytes
) -> None:
    counter = 0

    def run(task: bytes, config: bytes = config_bytes) -> RunRecord:
        nonlocal counter
        counter += 1
        run_id = f"20260828T0707{counter:02d}Z-aaaaaaaaaa"
        service = DialecticService(
            RunStore(tmp_path / f"state-{counter}", run_id_factory=lambda: run_id)
        )
        return asyncio.run(
            service.execute_code_once(
                service.create_run("code"),
                config_bytes=config,
                task_bytes=task,
                repository_path=tmp_path,
            )
        )

    valid = run("question 😀".encode("utf-8"))
    assert valid.failure_kind == "PREFLIGHT_FAILED"  # validation passed; Slice 0 has no executor
    for invalid in (
        b"\xef\xbb\xbftext",
        "text".encode("utf-16"),
        b"\xff",
    ):
        assert run(invalid).failure_kind == "INVALID_INPUT"
    surrogate_config = config_bytes.replace(b"codex-model", b'"\\uD800"', 1)
    assert run(b"task", surrogate_config).failure_kind == "INVALID_INPUT"


def test_core_030_capability_evidence_and_binding_barriers_are_fail_closed(
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / ".dialectic-turn"
    scratch_control = scratch_root / "control"
    scratch_tmp = scratch_root / "tmp"
    scratch_control.mkdir(parents=True)
    scratch_tmp.mkdir()
    driver_paths = {
        "state_root": tmp_path,
        "turn_scratch_root": scratch_root,
        "turn_scratch_control": scratch_control,
        "turn_scratch_tmp": scratch_tmp,
    }
    fixture = CapabilityFixture(
        probe_ids=("network-denied",),
        dynamic_roles=tuple(driver_paths),
        template={
            "rules": [
                {"role": role, "path": {"dynamic_path": role}}
                for role in driver_paths
            ]
        },
    )
    probe = CapabilityProbeResult(
        probe_id="network-denied",
        expected="deny",
        observed="denied",
        passed=True,
        bounded_diagnostic=None,
    )
    results_data = [probe.model_dump(mode="json")]
    results_hash = hashlib.sha256(
        (json.dumps(results_data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    attestation = CapabilityAttestationArtifact(
        **controller_fields(),
        runtime="codex",
        executable_identity="file-id",
        executable_sha256="a" * 64,
        spawned_root_identity="root-id",
        spawned_root_sha256="b" * 64,
        cli_version="1.0",
        platform_backend="windows-job",
        elevation_state="standard",
        adapter_fixture_version="1",
        fixture_test_version="1",
        profile_template_sha256=fixture.template_sha256,
        managed_policy_sha256="c" * 64,
        probe_results=[probe],
        probe_results_sha256=results_hash,
    )
    expected_fields = {
        "runtime": "codex",
        "executable_identity": "file-id",
        "cli_version": "1.0",
    }
    attestation_bytes = canonical_json_bytes(attestation)
    evidence_store = RunStore(tmp_path / "capability-state")
    cache_key = "d" * 64
    assert evidence_store.read_capability_attestation(cache_key) is None
    evidence_store.write_capability_attestation(cache_key, attestation)
    assert evidence_store.read_capability_attestation(cache_key) == attestation_bytes
    assert (
        validate_cached_attestation(
            attestation_bytes,
            fixture=fixture,
            expected_fields=expected_fields,
        )
        == attestation
    )
    with pytest.raises(CapabilityEvidenceError, match="stale"):
        validate_cached_attestation(
            attestation_bytes,
            fixture=fixture,
            expected_fields={**expected_fields, "cli_version": "2.0"},
        )
    probe_calls = 0

    def reprobe() -> CapabilityAttestationArtifact:
        nonlocal probe_calls
        probe_calls += 1
        return attestation

    validate_or_probe_attestation(
        b"corrupt",
        fixture=fixture,
        expected_fields=expected_fields,
        probe=reprobe,
    )
    assert probe_calls == 1

    target = AgentTarget(runtime="codex", model="codex-model", effort=None)
    preflight = TargetPreflightArtifact(
        **controller_fields(),
        role="driver",
        target_id="driver",
        target=target,
        resolved_executable="codex.exe",
        resolved_executable_identity="file-id",
        resolved_executable_sha256="a" * 64,
        spawned_root_executable="codex.exe",
        spawned_root_identity="root-id",
        spawned_root_sha256="b" * 64,
        launch_kind="direct",
        cli_version="1.0",
        prompt_transport="stdin",
        effective_static_flags=[],
        credential_env_names=[],
        denied_credential_path_sha256s=[],
        adapter_fixture_version="1",
        capability_attestation_sha256=hashlib.sha256(attestation_bytes).hexdigest(),
        authentication_verified=True,
    )
    preflight_bytes = canonical_json_bytes(preflight)
    concrete = {
        "rules": [
            {"role": role, "path": str(path.resolve())}
            for role, path in driver_paths.items()
        ]
    }
    binding = build_capability_binding(
        binding_id="driver-initial",
        role="driver",
        target_id="driver",
        access_mode="driver-write",
        target_preflight_bytes=preflight_bytes,
        attestation_bytes=attestation_bytes,
        attestation=attestation,
        fixture=fixture,
        dynamic_paths=driver_paths,
        supplied_concrete_profile=concrete,
    )
    assert binding.canonical_instantiation_verified is True
    assert {
        identity.role for identity in binding.dynamic_filesystem_identities
    }.issuperset(
        {"turn_scratch_root", "turn_scratch_control", "turn_scratch_tmp"}
    )
    with pytest.raises(CapabilityEvidenceError, match="role set"):
        build_capability_binding(
            binding_id="driver-incomplete",
            role="driver",
            target_id="driver",
            access_mode="driver-write",
            target_preflight_bytes=preflight_bytes,
            attestation_bytes=attestation_bytes,
            attestation=attestation,
            fixture=fixture,
            dynamic_paths={"state_root": tmp_path},
            supplied_concrete_profile=concrete,
        )

    neutral_dir = tmp_path / "reviewer-a"
    neutral_dir.mkdir()
    packet_fixture = CapabilityFixture(
        probe_ids=("network-denied",),
        dynamic_roles=("neutral_role_dir",),
        template={
            "rules": [
                {
                    "role": "neutral_role_dir",
                    "path": {"dynamic_path": "neutral_role_dir"},
                }
            ]
        },
    )
    packet_attestation = attestation.model_copy(
        update={"profile_template_sha256": packet_fixture.template_sha256}
    )
    packet_attestation_bytes = canonical_json_bytes(packet_attestation)
    packet_preflight = preflight.model_copy(
        update={
            "role": "reviewer",
            "target_id": "reviewer-a",
            "capability_attestation_sha256": hashlib.sha256(
                packet_attestation_bytes
            ).hexdigest(),
        }
    )
    packet_binding = build_capability_binding(
        binding_id="reviewer-a-review",
        role="reviewer",
        target_id="reviewer-a",
        access_mode="packet-only",
        target_preflight_bytes=canonical_json_bytes(packet_preflight),
        attestation_bytes=packet_attestation_bytes,
        attestation=packet_attestation,
        fixture=packet_fixture,
        dynamic_paths={"neutral_role_dir": neutral_dir},
        supplied_concrete_profile={
            "rules": [
                {
                    "role": "neutral_role_dir",
                    "path": str(neutral_dir.resolve()),
                }
            ]
        },
    )
    assert [
        identity.role for identity in packet_binding.dynamic_filesystem_identities
    ] == ["neutral_role_dir"]
    scratch_identity = next(
        identity
        for identity in binding.dynamic_filesystem_identities
        if identity.role == "turn_scratch_root"
    )
    with pytest.raises(ValidationError, match="scratch or isolated-worktree"):
        CapabilityBindingArtifact.model_validate(
            packet_binding.model_copy(
                update={
                    "dynamic_filesystem_identities": [
                        *packet_binding.dynamic_filesystem_identities,
                        scratch_identity,
                    ]
                }
            ).model_dump()
        )

    barrier = BindingBarrier(["driver", "reviewer"])
    barrier.add("driver", binding)
    with pytest.raises(CapabilityEvidenceError, match="closed"):
        barrier.authorize_launch()
    barrier.add("reviewer", packet_binding)
    assert len(barrier.authorize_launch()) == 2

    scratch_control.rmdir()
    scratch_tmp.rmdir()
    scratch_root.rmdir()
    scratch_control.mkdir(parents=True)
    scratch_tmp.mkdir()
    with pytest.raises(CapabilityEvidenceError, match="identity changed"):
        validate_binding_identities(
            binding,
            dynamic_paths=driver_paths,
            platform_backend=attestation.platform_backend,
        )

    names = {
        name.replace("test_core_", "CORE-")[:8].upper()
        for name in globals()
        if name.startswith("test_core_")
    }
    assert names == {f"CORE-{index:03d}" for index in range(1, 31)}
