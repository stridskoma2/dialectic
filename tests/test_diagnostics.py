from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from dialectic.capabilities import CapabilityFixture
from dialectic.cli import create_app
from dialectic.contracts import ARTIFACT_SCHEMA_VERSION, TOOL_VERSION
from dialectic.launcher import DirectLaunchSpec
from dialectic.native_adapters import NativePreflightMaterial
from dialectic.native_runtime import NativeDoctor
from dialectic.redaction import KnownCredential, KnownCredentials
from dialectic.schemas import (
    AgentTarget,
    CapabilityAttestationArtifact,
    CapabilityProbeResult,
    DoctorReport,
    DoctorTargetReport,
    PreflightResult,
)
from dialectic.service import DialecticService
from dialectic.store import RunStore


class _ReadyAdapter:
    def __init__(self, target: AgentTarget, material: NativePreflightMaterial) -> None:
        self.target = target
        self._material = material

    async def preflight(self, target: AgentTarget) -> PreflightResult:
        assert target == self.target
        return PreflightResult(
            target=target,
            requested_model=target.model,
            resolved_requested_model=target.model,
            actual_model=None,
            authentication_verified=True,
        )

    def preflight_material(self) -> NativePreflightMaterial:
        return self._material


class _FailingAdapter:
    def __init__(self, target: AgentTarget, diagnostic: str) -> None:
        self.target = target
        self.diagnostic = diagnostic

    async def preflight(self, target: AgentTarget) -> PreflightResult:
        assert target == self.target
        raise RuntimeError(self.diagnostic)


def _preflight_material(tmp_path: Path) -> NativePreflightMaterial:
    fixture = CapabilityFixture(
        probe_ids=("doctor-probe",),
        dynamic_roles=("neutral_role_dir",),
        template={"access_mode": "packet-only"},
    )
    probe = CapabilityProbeResult(
        probe_id="doctor-probe",
        expected="allow",
        observed="allowed",
        passed=True,
        bounded_diagnostic=None,
    )
    attestation = CapabilityAttestationArtifact(
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        tool_version=TOOL_VERSION,
        runtime="codex",
        executable_identity="volume:file",
        executable_sha256="a" * 64,
        spawned_root_identity="volume:file",
        spawned_root_sha256="a" * 64,
        cli_version="test-1",
        platform_backend="test",
        elevation_state="standard",
        adapter_fixture_version="test-fixture-v1",
        fixture_test_version="test-probe-v1",
        profile_template_sha256=fixture.template_sha256,
        managed_policy_sha256="b" * 64,
        probe_results=[probe],
        probe_results_sha256="c" * 64,
    )
    executable = tmp_path / "codex.exe"
    return NativePreflightMaterial(
        launch_plan=DirectLaunchSpec(executable, ()),
        resolved_executable=executable,
        resolved_executable_identity="volume:file",
        resolved_executable_sha256="a" * 64,
        spawned_root_executable=executable,
        spawned_root_identity="volume:file",
        spawned_root_sha256="a" * 64,
        cli_version="test-1",
        effective_static_flags=("--safe",),
        trusted_environment={},
        credential_environment_names=(),
        denied_credential_path_sha256s=(),
        fixture=fixture,
        adapter_fixture_version="test-fixture-v1",
        prompt_transport="stdin",
        process_lifecycle="per-turn",
        process_local_continuation=False,
        attestation=attestation,
    )


@pytest.mark.asyncio
async def test_doctor_uses_active_mode_targets_and_native_preflight(
    tmp_path: Path, config_bytes: bytes
) -> None:
    material = _preflight_material(tmp_path)
    calls: list[tuple[str, str]] = []

    def factory(target: AgentTarget, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((kwargs["role"], kwargs["access_mode"]))
        return _ReadyAdapter(target, material)

    store = RunStore(tmp_path / "state")
    service = DialecticService(
        store,
        doctor_executor=NativeDoctor(source_environment={}, adapter_factory=factory),
    )
    report = await service.doctor(config_bytes=config_bytes, mode="code")

    assert report.healthy
    assert [(target.role, target.target_id) for target in report.targets] == [
        ("driver", "driver"),
        ("reviewer", "self-review"),
    ]
    assert calls == [("driver", "driver-write"), ("reviewer", "packet-only")]
    assert all(target.authentication_verified for target in report.targets)
    assert all(target.capability_attestation_sha256 for target in report.targets)
    assert list(store.runs_root.iterdir()) == []


@pytest.mark.asyncio
async def test_native_doctor_collects_and_redacts_target_failures(
    tmp_path: Path, config_bytes: bytes
) -> None:
    material = _preflight_material(tmp_path)
    secret = "doctor-secret-value"
    calls = 0

    def factory(target: AgentTarget, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return _ReadyAdapter(target, material)
        return _FailingAdapter(target, f"preflight exposed {secret}")

    service = DialecticService(
        RunStore(tmp_path / "state"),
        credential_provider=lambda _config, _mode: KnownCredentials(
            [KnownCredential("TEST_TOKEN", secret)]
        ),
        doctor_executor=NativeDoctor(source_environment={}, adapter_factory=factory),
    )
    report = await service.doctor(config_bytes=config_bytes, mode="code")
    assert not report.healthy
    assert report.targets[0].ready
    assert report.targets[1].diagnostic == "preflight exposed [REDACT]"
    assert secret not in report.model_dump_json()


def test_doctor_cli_reports_failures_and_supports_json(
    tmp_path: Path, config_bytes: bytes
) -> None:
    config = tmp_path / "dialectic.yaml"
    config.write_bytes(config_bytes)

    async def doctor_executor(context):  # type: ignore[no-untyped-def]
        target = AgentTarget(runtime="codex", model="codex-model", effort="high")
        return context_report(
            context,
            DoctorTargetReport(
                role="driver",
                target_id="driver",
                target=target,
                access_mode="driver-write",
                ready=False,
                resolved_requested_model=None,
                resolved_executable=None,
                cli_version=None,
                adapter_fixture_version=None,
                prompt_transport=None,
                process_lifecycle=None,
                capability_attestation_sha256=None,
                authentication_verified=False,
                diagnostic="executable is unavailable: codex",
            ),
        )

    service = DialecticService(
        RunStore(tmp_path / "state"),
        doctor_executor=doctor_executor,
    )
    result = CliRunner().invoke(
        create_app(lambda: service),
        ["doctor", "--config", str(config), "--mode", "code", "--json"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["healthy"] is False
    assert payload["targets"][0]["diagnostic"] == "executable is unavailable: codex"
    assert list(service.store.runs_root.iterdir()) == []


def context_report(context, target: DoctorTargetReport):  # type: ignore[no-untyped-def]
    return DoctorReport(
        tool_version=TOOL_VERSION,
        mode=context.mode,
        config_sha256=context.config_sha256,
        state_root=str(context.store.state_root),
        healthy=target.ready,
        targets=[target],
    )


def _failed_run(tmp_path: Path) -> tuple[DialecticService, str]:
    run_id = "20260905T010101Z-aaaaaaaaaa"
    service = DialecticService(
        RunStore(tmp_path / "state", run_id_factory=lambda: run_id)
    )
    handle = service.create_run("code")
    service.fail_run(handle, "PREFLIGHT_FAILED", "expected test failure", phase="PREFLIGHT")
    return service, run_id


def test_offline_audit_validates_terminal_run_without_writing(tmp_path: Path) -> None:
    service, run_id = _failed_run(tmp_path)
    run_dir = service.run_artifact_directory(run_id)
    before = {
        path.relative_to(run_dir).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in run_dir.rglob("*")
        if path.is_file()
    }

    report = service.audit_run(run_id)

    after = {
        path.relative_to(run_dir).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    assert report.valid and report.complete
    assert report.status == "FAILED"
    assert report.events_checked == 1
    assert report.manifest_sha256 is not None
    assert report.issues == []
    assert after == before


def test_offline_audit_detects_event_summary_and_finalized_evidence_tampering(
    tmp_path: Path,
) -> None:
    service, run_id = _failed_run(tmp_path)
    run_dir = service.run_artifact_directory(run_id)
    event = json.loads((run_dir / "events.jsonl").read_text(encoding="utf-8"))
    event["sequence"] = 2
    (run_dir / "events.jsonl").write_text(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    summary["artifact_paths"]["escape"] = "../outside"
    (run_dir / "summary.json").write_bytes(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    )

    report = service.audit_run(run_id)
    codes = {issue.code for issue in report.issues}
    assert not report.valid and not report.complete
    assert {"EVENT_SEQUENCE_GAP", "SUMMARY_PATH_UNSAFE"} <= codes

    finalized_id = "20260905T010102Z-aaaaaaaaaa"
    finalized_service = DialecticService(
        RunStore(tmp_path / "finalized-state", run_id_factory=lambda: finalized_id)
    )
    handle = finalized_service.create_run("code")
    finalized_service.start_run(handle, phase="PREFLIGHT")
    finalized_service.finalize_code(handle, "COMPLETED_NO_FINDINGS")
    finalized_report = finalized_service.audit_run(finalized_id)
    assert not finalized_report.valid
    assert any(
        issue.code == "FINALIZED_ARTIFACT_MISSING"
        for issue in finalized_report.issues
    )


def test_offline_audit_rejects_hard_links_and_partial_runs_remain_read_only(
    tmp_path: Path,
) -> None:
    run_id = "20260905T010103Z-aaaaaaaaaa"
    service = DialecticService(
        RunStore(tmp_path / "state", run_id_factory=lambda: run_id)
    )
    handle = service.create_run("council")
    partial = service.audit_run(run_id)
    assert partial.valid and not partial.complete and partial.status == "CREATED"

    original = handle.path / "unexpected.txt"
    linked = handle.path / "linked.txt"
    original.write_bytes(b"not controller evidence")
    try:
        os.link(original, linked)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    report = service.audit_run(run_id)
    assert not report.valid
    assert any(issue.code == "HARD_LINKED_ARTIFACT" for issue in report.issues)


def test_offline_audit_fails_closed_when_issue_limit_is_reached(tmp_path: Path) -> None:
    run_id = "20260905T010104Z-aaaaaaaaaa"
    service = DialecticService(
        RunStore(tmp_path / "state", run_id_factory=lambda: run_id)
    )
    handle = service.create_run("code")
    for index in range(257):
        (handle.path / f"unknown-{index:03d}.json").write_bytes(b"{}")

    report = service.audit_run(run_id)

    assert not report.valid
    assert len(report.issues) == 256
    assert report.issues[-1].code == "ISSUE_LIMIT_REACHED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX link audit variant")
def test_offline_audit_rejects_symlink_evidence(tmp_path: Path) -> None:
    service, run_id = _failed_run(tmp_path)
    run_dir = service.run_artifact_directory(run_id)
    (run_dir / "unsafe-link").symlink_to(run_dir / "run.json")
    report = service.audit_run(run_id)
    assert not report.valid
    assert any(issue.code == "LINK_OR_REPARSE_ARTIFACT" for issue in report.issues)


def test_audit_cli_exit_codes_and_json(tmp_path: Path) -> None:
    service, run_id = _failed_run(tmp_path)
    app = create_app(lambda: service)
    valid = CliRunner().invoke(app, ["audit", run_id, "--json"])
    assert valid.exit_code == 0
    assert json.loads(valid.output)["valid"] is True

    invalid_id = CliRunner().invoke(app, ["audit", "../bad"])
    assert invalid_id.exit_code == 2
    assert "canonical grammar" in invalid_id.output

    run_dir = service.run_artifact_directory(run_id)
    (run_dir / "events.jsonl").write_bytes(b"not-json\n")
    invalid = CliRunner().invoke(app, ["audit", run_id])
    assert invalid.exit_code == 3
    assert "INVALID" in invalid.output
    assert "EVENT_SCHEMA_INVALID" in invalid.output


def test_doctor_rejects_invalid_configuration_without_native_calls(
    tmp_path: Path, config_data: dict[str, object]
) -> None:
    config_data.pop("driver")
    invoked = False

    async def doctor_executor(_context):  # type: ignore[no-untyped-def]
        nonlocal invoked
        invoked = True
        raise AssertionError("invalid configuration reached native doctor")

    service = DialecticService(
        RunStore(tmp_path / "state"), doctor_executor=doctor_executor
    )
    config = tmp_path / "invalid.yaml"
    config.write_bytes(yaml.safe_dump(config_data).encode("utf-8"))
    result = CliRunner().invoke(
        create_app(lambda: service),
        ["doctor", "--config", str(config), "--mode", "code"],
    )
    assert result.exit_code == 2
    assert "driver is required" in result.output
    assert not invoked


def test_diagnostic_commands_are_visible_in_cli_help(tmp_path: Path) -> None:
    service = DialecticService(RunStore(tmp_path / "state"))
    result = CliRunner().invoke(create_app(lambda: service), ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output
    assert "audit" in result.output
