from __future__ import annotations

import asyncio
import copy
import ctypes
import hashlib
import io
import json
import logging
import os
import socket
import subprocess
import sys
import sysconfig
import textwrap
import threading
import time
from types import SimpleNamespace
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
from dialectic.filesystem import stable_filesystem_identity
from dialectic.json_schema import JsonSchemaError, validate_json_schema
from dialectic.launcher import LaunchPlanError, WindowsBatchLaunchSpec, build_launch_spec
from dialectic.locking import (
    RepositoryBusyError,
    RepositoryLock,
    resolve_repository_identity,
)
from dialectic.output import OutputError, extract_model_payload
from dialectic.native_adapters import NativeTurnError
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
from dialectic.research import extract_source_citations, research_policy
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
    StreamCaptureResult,
    TargetPreflightArtifact,
    TurnAttemptArtifact,
    WorkspaceRecord,
)
from dialectic.scratch import (
    ScratchCleanupTimeout,
    ScratchContainmentError,
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
from dialectic.turn_timing import TurnDeadlineController, TurnDeadlineExpired
from dialectic.workflow_evidence import (
    bounded_preflight_diagnostic,
    redacted_response,
    stage_preflight_requests,
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

    deeply_nested = ("limits: " + "{a:" * 70 + "0" + "}" * 70).encode()
    with pytest.raises(ConfigError, match="configuration nesting exceeds 64"):
        ConfigLoader().load(deeply_nested)


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

    response = make_response().model_copy(
        update={
            "structured_output": {"secretvalue": {"nested": "secretvalue"}},
            "usage": {"secretvalue": 1},
        }
    )
    persisted_response = redacted_response(
        response, SimpleNamespace(credentials=credentials)
    )
    assert persisted_response.structured_output == {
        "[REDACT]": {"nested": "[REDACT]"}
    }
    assert persisted_response.usage == {"[REDACT]": 1}


@pytest.mark.asyncio
async def test_core_007_timeout_reaps_owned_descendant_before_sentinel(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
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

    class GracefulRootWithStubbornChild(FakeProcessUnit):
        async def _run_root(self) -> int:
            while not self.graceful_requested:
                await asyncio.sleep(0)
            return 0

    graceful_root = GracefulRootWithStubbornChild(root_delay=1, sentinel_delay=0.2)
    graceful_result = await ProcessSupervisor().supervise(
        graceful_root, turn_timeout_seconds=0.01, graceful_kill_seconds=0.02
    )
    assert graceful_result.cleanup_confirmed
    assert graceful_root.forced

    request = AgentRequest.model_construct(
        role="participant",
        target_id="participant-a",
        turn_phase="opening",
        prompt="test",
        output_schema=None,
        timeout_seconds=0.2,
        working_directory=".",
        access_mode="packet-only",
    )
    controller = TurnDeadlineController(idle_seconds=0.2, maximum_turn_seconds=3.0)

    async def completes_after_extension() -> str:
        await asyncio.sleep(0.3)
        return "completed"

    extended = asyncio.create_task(
        controller.wait_for(request, "claude-code", completes_after_extension())
    )
    await asyncio.sleep(0.01)
    assert controller.snapshot()["remainingSeconds"] > 0
    assert controller.extend_active(0.3)["extendedTurns"] == 1
    assert await extended == "completed"

    ceiling_request = request.model_copy(
        update={"target_id": "participant-ceiling", "timeout_seconds": 3.0}
    )
    at_ceiling = asyncio.create_task(
        controller.wait_for(ceiling_request, "claude-code", asyncio.sleep(0.01))
    )
    await asyncio.sleep(0)
    assert controller.snapshot()["canExtend"] is False
    assert controller.extend_active(0.03)["extendedTurns"] == 0
    await at_ceiling

    streaming_request = request.model_copy(update={"timeout_seconds": 1.0})
    activity = controller.activity_callback(streaming_request)

    async def streaming_response() -> str:
        for _ in range(3):
            await asyncio.sleep(0.02)
            activity()
        return "streamed"

    assert (
        await controller.wait_for(streaming_request, "codex", streaming_response())
        == "streamed"
    )

    with pytest.raises(TurnDeadlineExpired, match="no observable provider activity") as error:
        await controller.wait_for(streaming_request, "codex", asyncio.Event().wait())
    assert error.value.reason == "idle"

    started = asyncio.Event()

    async def ongoing() -> None:
        started.set()
        await asyncio.Event().wait()

    async def executor(context):  # type: ignore[no-untyped-def]
        await context.turn_deadlines.wait_for(
            request.model_copy(update={"timeout_seconds": 600}), "claude-code", ongoing()
        )

    service = DialecticService(RunStore(tmp_path / "extension"), code_executor=executor)
    handle = service.create_run("code")
    execution = asyncio.create_task(service.execute_code_once(
        handle, config_bytes=yaml.safe_dump(config_data).encode(),
        task_bytes=b"extension test", repository_path=tmp_path,
    ))
    await started.wait()
    snapshot = service.extend_turn_deadlines(handle.run_id)
    assert snapshot["extendedTurns"] == 1
    assert snapshot["remainingSeconds"] > 899
    execution.cancel()
    assert (await execution).status == "CANCELLED"

    long_request = request.model_copy(update={"timeout_seconds": 1})
    for runtime, elapsed in (("claude-code", 2.0), ("codex", 0.6)):
        clock = [0.0]
        expired = TurnDeadlineController(idle_seconds=0.5, monotonic=lambda: clock[0])
        pending = asyncio.get_running_loop().create_future()
        waiter = asyncio.create_task(expired.wait_for(long_request, runtime, pending))
        await asyncio.sleep(0)
        clock[0] = elapsed
        expired.activity_callback(long_request)()
        assert expired.snapshot()["canExtend"] is False
        assert expired.extend_active()["extendedTurns"] == 0
        service._active_deadlines[handle.run_id] = expired
        with pytest.raises(ValueError, match="^no active turn is eligible for extension$"):
            service.extend_turn_deadlines(handle.run_id)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    completed = TurnDeadlineController()
    pending = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(completed.wait_for(long_request, "codex", pending))
    await asyncio.sleep(0)
    pending.set_result("done")
    assert completed.extend_active()["extendedTurns"] == 0
    service._active_deadlines[handle.run_id] = completed
    with pytest.raises(ValueError, match="^no active turn is eligible for extension$"):
        service.extend_turn_deadlines(handle.run_id)
    assert await waiter == "done"


@pytest.mark.asyncio
async def test_core_008_cancellation_reaps_concurrent_units(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
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

    model_work_started = asyncio.Event()

    async def cancellable_executor(_context):  # type: ignore[no-untyped-def]
        model_work_started.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled executor resumed")

    store = RunStore(
        tmp_path / "cancelled-state",
        run_id_factory=lambda: "20260828T010008Z-aaaaaaaaaa",
    )
    service = DialecticService(store, code_executor=cancellable_executor)
    handle = service.create_run("code")
    execution = asyncio.create_task(
        service.execute_code_once(
            handle,
            config_bytes=yaml.safe_dump(config_data).encode("utf-8"),
            task_bytes=b"cancel this run",
            repository_path=tmp_path,
        )
    )
    await model_work_started.wait()
    execution.cancel()
    cancelled = await execution
    assert cancelled.status == "CANCELLED"
    assert exit_code_for(cancelled.status, cancelled.failure_kind) == 130
    assert service.get_result(handle.run_id).status == "CANCELLED"
    assert b'"event_type":"run-cancelled"' in (handle.path / "events.jsonl").read_bytes()


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
            if case_index == 1:
                assert "PREFLIGHT  RUNNING" in result.output
                assert "PREFLIGHT  FAILED" in result.output
            record = store.read_run(run_id)
            tree = sorted(
                str(path.relative_to(state / "runs" / record.run_id))
                for path in (state / "runs" / record.run_id).rglob("*")
            )
            outcomes.append((result.exit_code, tree, record.status, record.failure_kind))
        assert outcomes[0] == outcomes[1]

    scripts_directory = Path(sysconfig.get_path("scripts"))
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

    now = datetime.now(UTC)
    empty_stream = StreamCaptureResult(
        configured_limit_bytes=256,
        accepted_pre_redaction_bytes=0,
        accepted_pre_redaction_sha256=hashlib.sha256(b"").hexdigest(),
        discarded_guard_bytes=0,
        discarded_guard_reason="none",
        truncated=False,
        persisted_bytes=0,
        persisted_sha256=hashlib.sha256(b"").hexdigest(),
        triggered_termination=False,
    )
    response = make_response()
    valid_attempt = {
        **controller_fields(),
        "role": "driver",
        "target_id": "driver",
        "turn_phase": "initial",
        "operation": "start",
        "request_artifact_sha256": "a" * 64,
        "target_preflight_artifact_sha256": "b" * 64,
        "capability_binding_artifact_sha256": "c" * 64,
        "started_at": now,
        "response_completed_at": now,
        "capture_completed_at": now,
        "process_origin": "spawned-for-attempt",
        "process_lifecycle": "per-turn",
        "process_unit_id": "abcdefghijklmnop",
        "process_exit_code": 0,
        "attempt_end_reason": "response-returned",
        "failure_kind": None,
        "process_disposition": "closed",
        "stdout": empty_stream.model_dump(),
        "stderr": empty_stream.model_dump(),
        "response": response.model_dump(),
        "bounded_diagnostic": None,
    }
    TurnAttemptArtifact.model_validate(valid_attempt)
    invalid_attempts = [
        {**valid_attempt, "process_origin": "none"},
        {**valid_attempt, "process_unit_id": "not-a-unit"},
        {
            **valid_attempt,
            "process_origin": "retained-from-prior-turn",
            "process_disposition": "retained-for-session",
            "process_exit_code": None,
        },
        {**valid_attempt, "response": None},
        {**valid_attempt, "process_exit_code": None},
        {
            **valid_attempt,
            "stdout": {
                **empty_stream.model_dump(),
                "discarded_guard_bytes": 1,
                "discarded_guard_reason": "epoch-boundary",
            },
        },
    ]
    for invalid in invalid_attempts:
        with pytest.raises(ValidationError):
            TurnAttemptArtifact.model_validate(invalid)

    persistent = {
        **valid_attempt,
        "role": "participant",
        "target_id": "participant-a",
        "turn_phase": "opening",
        "process_lifecycle": "persistent-acp-session",
        "process_exit_code": None,
        "process_disposition": "retained-for-session",
    }
    TurnAttemptArtifact.model_validate(persistent)
    with pytest.raises(ValidationError):
        TurnAttemptArtifact.model_validate(
            {
                **persistent,
                "turn_phase": "cross-examination",
                "process_origin": "spawned-for-attempt",
            }
        )
    with pytest.raises(ValidationError):
        TurnAttemptArtifact.model_validate(
            {**persistent, "turn_phase": "ballot"}
        )

    persistent_preflight = {
        **controller_fields(),
        "role": "participant",
        "target_id": "participant-a",
        "target": {"runtime": "grok-build", "model": "grok-model", "effort": None},
        "resolved_executable": "grok.exe",
        "resolved_executable_identity": "grok-id",
        "resolved_executable_sha256": "d" * 64,
        "spawned_root_executable": "grok.exe",
        "spawned_root_identity": "grok-id",
        "spawned_root_sha256": "d" * 64,
        "launch_kind": "direct",
        "cli_version": "0.1.220",
        "prompt_transport": "acp-stdio",
        "process_lifecycle": "persistent-acp-session",
        "effective_static_flags": [],
        "credential_env_names": [],
        "denied_credential_path_sha256s": [],
        "adapter_fixture_version": "grok-0.1.220-acp-v1",
        "capability_attestation_sha256": "e" * 64,
        "authentication_verified": True,
    }
    TargetPreflightArtifact.model_validate(persistent_preflight)
    for mutation in (
        {"role": "moderator"},
        {"prompt_transport": "stdin"},
    ):
        with pytest.raises(ValidationError):
            TargetPreflightArtifact.model_validate({**persistent_preflight, **mutation})
    TargetPreflightArtifact.model_validate(
        {
            **persistent_preflight,
            "target": {"runtime": "codex", "model": "future-acp", "effort": None},
            "adapter_fixture_version": "future-process-local-acp-v1",
        }
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

    role_store = RunStore(
        tmp_path / "role-state",
        role_directories_root=tmp_path / "external-role-directories",
        run_id_factory=lambda: "20260828T020206Z-aaaaaaaaaa",
    )
    role_handle = role_store.bootstrap_run("council")
    role_directory = role_store.create_role_directory(
        role_handle, "council-role-directories", "participant-a"
    )
    assert role_directory == (
        role_store.role_directories_root
        / role_handle.run_id
        / "council-role-directories"
        / "participant-a"
    ).resolve(strict=True)
    assert not role_directory.is_relative_to(role_handle.path)
    with pytest.raises(FileExistsError, match="already exists"):
        role_store.create_role_directory(
            role_handle, "council-role-directories", "participant-a"
        )


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
    scenarios: list[tuple[FakeProcessUnit, asyncio.Event | None, asyncio.Event | None]] = [
        (FakeProcessUnit(root_delay=0, cleanup_confirmed=False), None, None),
        (FakeProcessUnit(root_delay=1, cleanup_confirmed=False), None, None),
    ]
    cancelled = asyncio.Event()
    cancelled.set()
    scenarios.append(
        (FakeProcessUnit(root_delay=1, cleanup_confirmed=False), cancelled, None)
    )
    failed_fast = asyncio.Event()
    failed_fast.set()
    scenarios.append(
        (FakeProcessUnit(root_delay=1, cleanup_confirmed=False), None, failed_fast)
    )
    for unit, cancellation, overflow in scenarios:
        result = await ProcessSupervisor().supervise(
            unit,
            turn_timeout_seconds=0.01,
            graceful_kill_seconds=0.01,
            cancellation=cancellation,
            overflow=overflow,
        )
        assert result.termination_reason == "cleanup-failed"
        assert result.failure_kind == "PROCESS_CLEANUP_FAILED"

    now = datetime.now(UTC)
    empty = StreamCaptureResult(
        configured_limit_bytes=256,
        accepted_pre_redaction_bytes=0,
        accepted_pre_redaction_sha256=hashlib.sha256(b"").hexdigest(),
        discarded_guard_bytes=0,
        discarded_guard_reason="none",
        truncated=False,
        persisted_bytes=0,
        persisted_sha256=hashlib.sha256(b"").hexdigest(),
        triggered_termination=False,
    )
    retained = TurnAttemptArtifact(
        **controller_fields(),
        role="participant",
        target_id="participant-a",
        turn_phase="opening",
        operation="start",
        request_artifact_sha256="a" * 64,
        target_preflight_artifact_sha256="b" * 64,
        capability_binding_artifact_sha256="c" * 64,
        started_at=now,
        response_completed_at=now,
        capture_completed_at=now,
        process_origin="spawned-for-attempt",
        process_lifecycle="persistent-acp-session",
        process_unit_id="abcdefghijklmnop",
        process_exit_code=None,
        attempt_end_reason="response-returned",
        failure_kind=None,
        process_disposition="retained-for-session",
        stdout=empty,
        stderr=empty,
        response=make_response(),
        bounded_diagnostic=None,
    )
    assert retained.process_disposition == "retained-for-session"
    assert retained.failure_kind is None

    overflow_capture = BoundedStreamCapture(
        256, KnownCredentials([KnownCredential("TOKEN", "secretvalue")])
    )
    overflow_capture.feed(b"x" * 257)
    failed_payload = retained.model_dump(mode="python")
    failed_payload.update(
        stdout=overflow_capture.finish().result,
        response=None, response_completed_at=None,
        attempt_end_reason="cleanup-failed", failure_kind="PROCESS_CLEANUP_FAILED",
        process_disposition="cleanup-failed",
    )
    failed = TurnAttemptArtifact.model_validate(failed_payload)
    assert failed.stdout.discarded_guard_reason == "overflow"
    assert failed.failure_kind == "PROCESS_CLEANUP_FAILED"

    for trigger in ("deadline", "cancel"):
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def cleanup_failure() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await finish_cleanup.wait()
                raise NativeTurnError("PROCESS_CLEANUP_FAILED", "owned child remains")

        request = AgentRequest.model_construct(
            role="participant", target_id="participant-a", turn_phase="opening",
            timeout_seconds=0.02, prompt="test", working_directory=".",
            access_mode="packet-only", output_schema=None,
        )
        controller = TurnDeadlineController()
        waiter = asyncio.create_task(controller.wait_for(request, "codex", cleanup_failure()))
        await asyncio.sleep(0.001)
        if trigger == "cancel":
            waiter.cancel()
        await cleanup_started.wait()
        assert controller.extend_active()["extendedTurns"] == 0
        waiter.cancel()  # Repeated cancellation must still drain native cleanup.
        finish_cleanup.set()
        with pytest.raises(NativeTurnError) as failure:
            await waiter
        assert failure.value.kind == "PROCESS_CLEANUP_FAILED"


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
            store.write_artifact(
                handle,
                "git/workspace.json",
                WorkspaceRecord(
                    **controller_fields(),
                    repo_common_dir=str(tmp_path / "repo" / ".git"),
                    repo_filesystem_identity="volume:file",
                    repo_lock_identity_sha256="a" * 64,
                    original_worktree=str(tmp_path / "repo"),
                    original_branch="main",
                    base_sha="b" * 40,
                    dialectic_branch=f"dialectic/{run_id}",
                    dialectic_worktree=str(tmp_path / "isolated"),
                    review_sha="c" * 40,
                    final_sha="c" * 40,
                    initial_diff_sha256="d" * 64,
                    repair_delta_sha256=None,
                    final_diff_sha256="d" * 64,
                ),
            )
            service.finalize_code(handle, "COMPLETED_NO_FINDINGS")
        else:
            service.fail_run(handle, "PREFLIGHT_FAILED", "expected")
        result = runner.invoke(create_app(lambda service=service: service), ["status", run_id])
        assert result.exit_code == 0
        assert str(service.run_artifact_directory(run_id)) in result.output
        if terminal == "FINALIZED":
            assert str(tmp_path / "isolated") in result.output
            assert f"dialectic/{run_id}" in result.output
            assert "checked-out files, index, branch, HEAD" in result.output
            assert "shared Git metadata and objects added" in result.output
            assert "git worktree remove" in result.output
            assert "git branch -D" in result.output
            assert "git worktree prune" in result.output
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
    tmp_path: Path,
    config_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    if os.name != "nt":
        aux = tmp_path / "aux.md"
        aux.write_bytes(b"ordinary POSIX file")
        assert acquire_named_file(aux, label="input") == b"ordinary POSIX file"
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

    async def unexpected_executor(_context: object) -> RunRecord:
        raise RuntimeError("private diagnostic detail")

    caplog.set_level(logging.ERROR, logger="dialectic.service")
    diagnostic_service = DialecticService(
        RunStore(
            tmp_path / "unexpected-state",
            run_id_factory=lambda: "20260828T062659Z-aaaaaaaaaa",
        ),
        code_executor=unexpected_executor,
    )
    diagnostic_record = asyncio.run(
        diagnostic_service.execute_code_once(
            diagnostic_service.create_run("code"),
            config_bytes=config_bytes,
            task_bytes=b"trigger controller diagnostic",
            repository_path=tmp_path,
        )
    )
    assert diagnostic_record.failure_kind == "INTERNAL_ERROR"
    diagnostic_log = next(
        record
        for record in caplog.records
        if getattr(record, "dialectic_event", "") == "controller.unexpected_error"
    )
    assert diagnostic_log.dialectic_fields["exception_type"] == "RuntimeError"
    assert "unexpected_executor" in diagnostic_log.dialectic_fields["stack"]
    assert "private diagnostic detail" not in diagnostic_log.dialectic_fields["stack"]

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
        (33, b"driver:\n  model: ${" + b"A" * 5000 + b"}\n", b"task"),
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
        assert len(record.failure_detail.encode("utf-8")) <= 4096

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


def test_core_027_stream_scratch_and_cleanup_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    epoch_one = BoundedStreamCapture(256, credentials)
    epoch_one.feed(b"opening:" + secret[:8].encode())
    first_epoch = epoch_one.finish(epoch_boundary=True)
    epoch_two = BoundedStreamCapture(256, credentials)
    epoch_two.feed(
        secret[8:].encode() + b":cross:moderator-idle:padding-padding-padding"
    )
    second_epoch = epoch_two.finish(epoch_boundary=True)
    epoch_three = BoundedStreamCapture(256, credentials)
    epoch_three.feed(b"ballot-final")
    final_epoch = epoch_three.finish()
    epochs = (first_epoch, second_epoch, final_epoch)
    assert [item.result.configured_limit_bytes for item in epochs] == [256, 256, 256]
    assert first_epoch.result.discarded_guard_bytes == min(
        len(secret.encode()) - 1,
        first_epoch.result.accepted_pre_redaction_bytes,
    )
    assert second_epoch.result.discarded_guard_reason == "epoch-boundary"
    assert final_epoch.result.discarded_guard_reason == "none"
    assert b"moderator-idle" in second_epoch.persisted
    assert secret.encode() not in b"".join(item.persisted for item in epochs)

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

    missing_root = tmp_path / "missing-scratch"
    with pytest.raises(ScratchContainmentError, match="scratch root cannot be inspected safely") as failure:
        scan_scratch(missing_root, ScratchLimits(100, 10, 3))
    assert isinstance(failure.value.__cause__, FileNotFoundError)

    original_lstat = Path.lstat

    def inaccessible_root(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == bytes_root:
            raise PermissionError("simulated root access failure")
        return original_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", inaccessible_root)
        with pytest.raises(ScratchContainmentError, match="scratch root cannot be inspected safely") as failure:
            scan_scratch(bytes_root, ScratchLimits(100, 10, 3))
        assert isinstance(failure.value.__cause__, PermissionError)

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

    if os.name != "nt":
        # Exercise the production depth ceiling with a 1024-fd limit. Lowering the
        # child's recursion limit still catches recursive walks, without requiring
        # a >1000-level tree that exceeds both production bounds and ordinary fd limits.
        # Both process limits stay isolated from pytest and its plugins.
        depth_probe = textwrap.dedent("""
            import resource
            import sys
            from pathlib import Path
            from dialectic.scratch import ScratchLimits, scan_scratch, cleanup_reserved_tree

            _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
            sys.setrecursionlimit(128)
            root = Path(sys.argv[1])
            root.mkdir()
            cursor = root
            for _ in range(256):
                cursor /= 'd'
                cursor.mkdir()
            try:
                for _ in range(3):
                    usage = scan_scratch(root, ScratchLimits(100, 1000, 256))
                    assert usage.within_limits
                    assert usage.maximum_depth == 256
                    assert scan_scratch(root, ScratchLimits(100, 1000, 128)).overage == 'depth'
            finally:
                cleanup_reserved_tree(root, timeout_seconds=10)
            assert not root.exists()
        """)
        result = subprocess.run(
            [sys.executable, "-c", depth_probe, str(tmp_path / "deep-cleanup")],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr

        original_open = os.open

        def inaccessible_child(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
            if path == "one":
                raise PermissionError("simulated child access failure")
            return original_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", inaccessible_child)
            with pytest.raises(ScratchContainmentError, match="scratch directory cannot be opened safely") as failure:
                scan_scratch(depth_root, ScratchLimits(100, 10, 3))
            assert isinstance(failure.value.__cause__, PermissionError)

        class InaccessibleEntry:
            def stat(self, *, follow_symlinks: bool):  # type: ignore[no-untyped-def]
                raise PermissionError("simulated entry identity failure")

        def inaccessible_entries(_directory_fd):  # type: ignore[no-untyped-def]
            yield InaccessibleEntry()

        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", inaccessible_entries)
            with pytest.raises(ScratchContainmentError, match="scratch entry identity changed during traversal") as failure:
                scan_scratch(depth_root, ScratchLimits(100, 10, 3))
            assert isinstance(failure.value.__cause__, PermissionError)


@pytest.mark.asyncio
async def test_core_028_windows_reader_handoff_is_byte_and_callback_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    controller = TurnDeadlineController()
    request = AgentRequest.model_construct(
        role="driver", target_id="driver", turn_phase="initial",
        timeout_seconds=60, prompt="test", working_directory=".",
        access_mode="driver-write", output_schema=None,
    )
    invocation = loop.create_future()
    waiter = asyncio.create_task(controller.wait_for(request, "codex", invocation))
    await asyncio.sleep(0)
    activity = controller.activity_callback(request)
    queued: list[object] = []
    original_schedule = loop.call_soon_threadsafe

    def track_schedule(callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        queued.append(callback)
        return original_schedule(callback, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(loop, "call_soon_threadsafe", track_schedule)
        small_reads = WindowsReaderHandoff(
            limit_bytes=20_000, queue_capacity_bytes=20_000,
            coordinator=ReaderHandoffCoordinator(), notify=lambda: None,
            loop=loop, on_activity=activity,
        )
        reader = threading.Thread(target=small_reads.read_pipe, args=(ChunkedPipe([b"x"] * 10_000),))
        reader.start()
        reader.join(timeout=5)  # Hold the loop still while the real reader emits activity.
        assert not reader.is_alive()
        assert len(queued) <= 3  # One activity wakeup plus data and terminal notifications.
        assert small_reads.peak_queued_bytes == 10_000
    invocation.set_result(None)
    await waiter
    activity()  # A callback retained by a reader is inert after deregistration.

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

    epoch_coordinator = ReaderHandoffCoordinator()
    epoch_handoffs = [
        WindowsReaderHandoff(
            limit_bytes=32,
            queue_capacity_bytes=32,
            coordinator=epoch_coordinator,
            notify=lambda: None,
        )
        for _ in range(2)
    ]
    epoch_threads = [
        threading.Thread(
            target=handoff.read_pipe,
            args=(ChunkedPipe([chunk]),),
            daemon=True,
        )
        for handoff, chunk in zip(epoch_handoffs, (b"stdout-epoch", b"stderr-epoch"))
    ]
    for reader in epoch_threads:
        reader.start()
    wait_until(lambda: all(handoff.completed for handoff in epoch_handoffs))
    switched = epoch_coordinator.switch_epoch(epoch_handoffs)
    assert switched == ([b"stdout-epoch"], [b"stderr-epoch"])
    assert all(handoff.epoch_accepted_bytes == 0 for handoff in epoch_handoffs)

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
                "stdin": (Resource("stdin-parent"), Resource("stdin-child")),
                "stdout": (Resource("stdout-parent"), Resource("stdout-child")),
                "stderr": (Resource("stderr-parent"), Resource("stderr-child")),
            }

        def create_attribute_list(self, *, job, inherited_handles):
            self.calls.append("attributes")
            assert job is self.job and len(inherited_handles) == 3
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
    assert len(backend.closed) == 10

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
    assert len(job_list_backend.closed) == 7

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
    assert len(failed_backend.closed) == 10

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
    assert len(blocking_backend.closed) == 10

    class UndeliverableGracefulBackend(BlockingBackend):
        def request_graceful_termination(self, process):
            super().request_graceful_termination(process)
            return False

    undeliverable_backend = UndeliverableGracefulBackend()
    undeliverable_unit = WindowsJobLauncher(undeliverable_backend).launch(
        executable="agent.exe",
        arguments=(),
        cwd="C:\\neutral",
        environment={"SystemRoot": "C:\\Windows"},
    )
    undeliverable_overflow = asyncio.Event()
    undeliverable_overflow.set()
    started = time.monotonic()
    undeliverable_result = await ProcessSupervisor().supervise(
        undeliverable_unit,
        turn_timeout_seconds=1,
        graceful_kill_seconds=0.5,
        overflow=undeliverable_overflow,
    )
    assert time.monotonic() - started < 0.25
    assert undeliverable_result.cleanup_confirmed
    assert "terminate-job" in undeliverable_backend.calls

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
    assert research_policy()["allowed_tools"] == ["web-search", "web-fetch"]
    citations, total = extract_source_citations(
        "[Foo (bar)](https://en.wikipedia.org/wiki/Foo_(bar)) and "
        "https://en.wikipedia.org/wiki/Baz_(qux).",
        limit=10,
    )
    assert total == 2
    assert [citation.url for citation in citations] == [
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://en.wikipedia.org/wiki/Baz_(qux)",
    ]

    cyclic_schema = {
        "$defs": {"loop": {"$ref": "#/$defs/loop"}},
        "$ref": "#/$defs/loop",
    }
    with pytest.raises(JsonSchemaError, match="cyclic"):
        validate_json_schema({}, cyclic_schema)

    if os.name != "nt":
        identity_target = tmp_path / "identity-target"
        identity_target.write_text("target", encoding="utf-8")
        identity_link = tmp_path / "identity-link"
        identity_link.symlink_to(identity_target)
        assert stable_filesystem_identity(identity_link) != stable_filesystem_identity(
            identity_target
        )

    leaders, followers = stage_preflight_requests(
        (
            ("participant-a", "codex", "packet-only"),
            ("participant-b", "claude-code", "packet-only"),
            ("participant-c", "claude-code", "packet-only"),
            ("driver", "codex", "driver-write"),
        ),
        cohort_key=lambda request: request[1:],
    )
    assert leaders == (
        ("participant-a", "codex", "packet-only"),
        ("participant-b", "claude-code", "packet-only"),
        ("driver", "codex", "driver-write"),
    )
    assert followers == (("participant-c", "claude-code", "packet-only"),)

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
    failed_probe = CapabilityProbeResult(
        probe_id="network-denied",
        expected="deny",
        observed="unavailable",
        passed=False,
        bounded_diagnostic="pinned probe",
    )
    failed_results = [failed_probe.model_dump(mode="json")]
    failed_attestation = attestation.model_copy(
        update={
            "probe_results": [failed_probe],
            "probe_results_sha256": hashlib.sha256(
                (
                    json.dumps(failed_results, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
            ).hexdigest(),
        }
    )
    with pytest.raises(
        CapabilityEvidenceError,
        match="network-denied=unavailable",
    ) as failed:
        validate_cached_attestation(
            canonical_json_bytes(failed_attestation),
            fixture=fixture,
            expected_fields=expected_fields,
        )
    assert bounded_preflight_diagnostic(failed.value) == (
        "capability attestation failed probes: network-denied=unavailable"
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
        process_lifecycle="per-turn",
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
    wrong_role_paths = dict(driver_paths)
    wrong_role_paths["turn_scratch_control"] = scratch_tmp
    wrong_role_paths["turn_scratch_tmp"] = scratch_control
    with pytest.raises(CapabilityEvidenceError, match="canonical template"):
        build_capability_binding(
            binding_id="driver-wrong-role",
            role="driver",
            target_id="driver",
            access_mode="driver-write",
            target_preflight_bytes=preflight_bytes,
            attestation_bytes=attestation_bytes,
            attestation=attestation,
            fixture=fixture,
            dynamic_paths=wrong_role_paths,
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

    displaced_scratch = tmp_path / ".dialectic-turn-displaced"
    scratch_root.rename(displaced_scratch)
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
