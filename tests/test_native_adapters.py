from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from dialectic.acp_transport import (
    AcpEpochCapture,
    AcpLogicalResponse,
    AcpProtocolError,
    ManagedAcpLeaseFactory,
)
from dialectic.capabilities import instantiate_capability_template
from dialectic.grok_acp import GrokAdapter
from dialectic.code_once import CodeOnceOrchestrator
from dialectic.native_adapters import (
    ClaudeAdapter,
    CodexAdapter,
    NativeEnvelopeError,
    NativePreflightError,
    NativeProcessResult,
    NativeTurnError,
    _versioned_fixture,
    recorded_probe_provider,
)
from dialectic.launcher import DirectLaunchSpec
from dialectic.native_process import BoundedNativeProcessTransport
from dialectic.redaction import BoundedStreamCapture, KnownCredential, KnownCredentials
from dialectic.schemas import AgentRequest, AgentTarget, CapabilityBindingArtifact
from dialectic.service import DialecticService
from dialectic.store import RunStore
from dialectic.workflow_evidence import bounded_preflight_diagnostic


class FakeNativeTransport:
    def __init__(self, outputs: list[tuple[bytes, bytes, int]]) -> None:
        self.outputs = deque(outputs)
        self.calls: list[dict[str, Any]] = []

    async def run(self, plan, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"plan": plan, **kwargs})
        stdout, stderr, exit_code = self.outputs.popleft()
        credentials = kwargs["credentials"]
        return NativeProcessResult(
            process_started=True,
            exit_code=exit_code,
            end_reason="completed",
            failure_kind=None,
            cleanup_confirmed=True,
            stdout=_capture(stdout, kwargs["stdout_limit"], credentials),
            stderr=_capture(stderr, kwargs["stderr_limit"], credentials),
        )


def _codex_doctor_report(version: str = "0.151.0-alpha.7.1") -> bytes:
    return json.dumps({
        "schemaVersion": 1,
        "codexVersion": version,
        "checks": {
            "config.load": {
                "status": "ok",
                "summary": "config loaded",
                "details": {
                    "mcp servers": "2",
                    "feature flag overrides": "multi_agent=false",
                },
            },
            "mcp.config": {
                "status": "ok",
                "summary": "MCP configuration is locally consistent",
                "details": {"configured servers": "2"},
            },
            "sandbox.helpers": {
                "status": "ok",
                "summary": "permission profile is enforceable",
                "details": {
                    "approval policy": "Never",
                    "denied-read restrictions": "true",
                    "filesystem sandbox": "restricted",
                    "network sandbox": "restricted",
                    "sandbox backend": "windows-job" if os.name == "nt" else "landlock",
                },
            },
        },
    }).encode()


class CodeOnceNativeTransport(FakeNativeTransport):
    def __init__(self, *, driver: bool) -> None:
        super().__init__([
            (b"codex-cli 0.150.0-alpha.12.2\n", b"", 0),
            (b"--json --output-schema --ignore-user-config --ignore-rules --strict-config --skip-git-repo-check\n", b"", 0),
            (b"SESSION_ID --json --output-schema --ignore-user-config --ignore-rules --strict-config --skip-git-repo-check\n", b"", 0),
            (b"Logged in\n", b"", 0),
            (_codex_doctor_report("0.150.0-alpha.12.2"), b"", 1),
        ])
        self.driver = driver

    async def run(self, plan, **kwargs):  # type: ignore[no-untyped-def]
        if self.outputs:
            return await super().run(plan, **kwargs)
        self.calls.append({"plan": plan, **kwargs})
        if self.driver:
            Path(kwargs["cwd"], "native.txt").write_text(
                "native adapter\n", encoding="utf-8"
            )
            text = "implemented"
            session = "driver-native-session"
        else:
            packet = json.loads(kwargs["stdin"].decode())
            core = packet["core"]
            text = json.dumps({
                "schema_version": 1,
                "base_sha": core["base_sha"],
                "head_sha": core["review_sha"],
                "verdict": "pass",
                "summary": "native reviewer passed",
                "findings": [],
            })
            session = "reviewer-native-session"
        stdout = (
            json.dumps({"type": "thread.started", "thread_id": session})
            + "\n"
            + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}})
            + "\n"
        ).encode()
        return NativeProcessResult(
            process_started=True,
            exit_code=0,
            end_reason="completed",
            failure_kind=None,
            cleanup_confirmed=True,
            stdout=_capture(stdout, kwargs["stdout_limit"], kwargs["credentials"]),
            stderr=_capture(b"", kwargs["stderr_limit"], kwargs["credentials"]),
        )


class FakeAcpLease:
    def __init__(
        self,
        *,
        session_id: str,
        process_unit_id: str,
        responses: list[str | BaseException],
        credentials: KnownCredentials,
    ) -> None:
        self.session_id = session_id
        self.process_unit_id = process_unit_id
        self.responses = deque(responses)
        self.credentials = credentials
        self.prompts: list[str] = []
        self.switch_count = 0
        self.close_count = 0
        self.boundary_error: BaseException | None = None
        self.close_data = b"final-secretvalue"
        self.cleanup_confirmed = True

    async def prompt(self, text: str, timeout_seconds: float) -> AcpLogicalResponse:
        self.prompts.append(text)
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return AcpLogicalResponse(
            session_id=self.session_id,
            text=response,
            actual_model="grok-model",
            usage={"turn": len(self.prompts)},
        )

    async def switch_epoch(self) -> AcpEpochCapture:
        self.switch_count += 1
        if self.boundary_error is not None:
            raise self.boundary_error
        data = (
            b"prefix-secret"
            if self.switch_count == 1
            else b"value-moderation-trailing-guard"
        )
        return _epoch(
            data,
            self.credentials,
            boundary=True,
            exit_code=None,
        )

    async def close(self, graceful_kill_seconds: float) -> AcpEpochCapture:
        self.close_count += 1
        return _epoch(
            self.close_data,
            self.credentials,
            boundary=False,
            exit_code=0,
            cleanup_confirmed=self.cleanup_confirmed,
        )


class FakeAcpFactory:
    def __init__(
        self,
        responses: list[list[str | BaseException]],
        settings: list[dict[str, Any]] | None = None,
    ) -> None:
        self.responses = deque(responses)
        self.settings = deque(settings or [{} for _ in responses])
        self.leases: list[FakeAcpLease] = []

    async def open(self, plan, **kwargs):  # type: ignore[no-untyped-def]
        lease = FakeAcpLease(
            session_id=f"grok-session-{len(self.leases) + 1}",
            process_unit_id=kwargs["process_unit_id"],
            responses=self.responses.popleft(),
            credentials=kwargs["credentials"],
        )
        for name, value in self.settings.popleft().items():
            setattr(lease, name, value)
        self.leases.append(lease)
        return lease


def _capture(data: bytes, limit: int, credentials: KnownCredentials):
    capture = BoundedStreamCapture(limit, credentials)
    capture.feed(data)
    return capture.finish()


def _epoch(
    data: bytes,
    credentials: KnownCredentials,
    *,
    boundary: bool,
    exit_code: int | None,
    cleanup_confirmed: bool = True,
) -> AcpEpochCapture:
    stdout = BoundedStreamCapture(1024, credentials)
    stdout.feed(data)
    stderr = BoundedStreamCapture(1024, credentials)
    return AcpEpochCapture(
        stdout=stdout.finish(epoch_boundary=boundary),
        stderr=stderr.finish(epoch_boundary=boundary),
        process_exit_code=exit_code,
        cleanup_confirmed=cleanup_confirmed,
    )


def _source_environment() -> dict[str, str]:
    if os.name == "nt":
        required = ("SystemRoot", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PATH")
    else:
        required = ("HOME", "PATH")
    return {name: os.environ[name] for name in required}


def _binding(  # type: ignore[no-untyped-def]
    adapter,
    target_id: str,
    phase: str,
    concrete: Mapping[str, Any],
    dynamic_paths: Mapping[str, Path] | None = None,
):
    binding = CapabilityBindingArtifact.model_construct(
        artifact_schema_version=1,
        tool_version="0.1.0",
        binding_id=f"run:{target_id}:{phase}",
        role=adapter.role,
        target_id=target_id,
        access_mode=adapter.access_mode,
        target_preflight_artifact_sha256="a" * 64,
        capability_attestation_sha256="b" * 64,
        profile_template_sha256="c" * 64,
        concrete_profile_sha256=hashlib.sha256(
            (json.dumps(concrete, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
        dynamic_filesystem_identities=[],
        canonical_instantiation_verified=True,
    )
    adapter.bind_capability(binding, concrete, dynamic_paths or {})


def _request(
    tmp_path: Path,
    *,
    phase: str,
    schema: dict[str, Any] | None = None,
    prompt: str = "complete prompt over stdin",
):
    return AgentRequest(
        role="reviewer",
        target_id="reviewer-a",
        turn_phase=phase,
        prompt=prompt,
        output_schema=schema,
        timeout_seconds=10,
        working_directory=str(tmp_path),
        access_mode="packet-only",
    )


def test_unqualified_native_version_error_names_the_installed_and_qualified_versions() -> None:
    with pytest.raises(NativePreflightError) as rejected:
        _versioned_fixture(
            "codex",
            "0.152.0",
            role="participant",
            access_mode="packet-only",
            source_environment={},
        )

    qualified = ["0.150.0-alpha.12.2", "0.151.0-alpha.7.1"]
    assert str(rejected.value) == (
        "Codex CLI 0.152.0 is installed but has not been qualified by Dialectic 0.1.0; "
        f"qualified versions: {', '.join(sorted(qualified))}. Install a qualified CLI "
        "version or upgrade Dialectic after support is added."
    )


def test_stable_codex_is_not_fixture_eligible() -> None:
    with pytest.raises(NativePreflightError):
        _versioned_fixture(
            "codex",
            "0.151.0",
            role="driver",
            access_mode="driver-write",
            source_environment={},
        )

    if os.name == "nt":
        with pytest.raises(NativePreflightError, match="qualified.*packet-only") as rejected:
            _versioned_fixture(
                "codex",
                "0.151.0-alpha.7.1",
                role="driver",
                access_mode="driver-write",
                source_environment={},
            )
        assert "required tmp writes and read-only Git inspection failed" in str(
            rejected.value
        )
        assert "0.150.0-alpha.12.2" in str(rejected.value)


def test_stable_codex_rejection_explains_failed_permission_matrix() -> None:
    for role, access_mode in (
        ("driver", "driver-write"),
        ("participant", "packet-only"),
    ):
        with pytest.raises(NativePreflightError) as rejected:
            _versioned_fixture(
                "codex",
                "0.151.0",
                role=role,
                access_mode=access_mode,
                source_environment={},
            )

        message = str(rejected.value)
        if os.name == "nt":
            assert "failed both Dialectic permission profiles" in message
            assert "did not preserve the isolated-worktree CWD" in message
            assert "private neutral CWD" in message
        else:
            assert "driver-write live permission matrix" in message
            assert "Bubblewrap could not mount" in message
            assert "AGENTS.md discovery was not preserved" in message
            assert "tool surface exceeded the qualified fixture" in message
        assert "No sandbox boundary was weakened" in message


def test_live_web_fixtures_expose_only_provider_web_tools() -> None:
    codex = _versioned_fixture(
        "codex",
        "0.151.0-alpha.7.1",
        role="participant",
        access_mode="packet-only",
        source_environment={},
        research_mode="live-web",
    )
    codex_template = codex.capability_fixture.template
    codex_profile = codex_template["permissions"]["dialectic-packet-web"]
    assert codex_template["web_search"] == "live"
    assert codex_profile["network"] == {"enabled": False}
    assert codex_template["apps"] == {"_default": {"enabled": False}}
    assert codex_template["mcp_servers"] == {}
    assert "web-research-allow" in codex.capability_fixture.probe_ids
    assert codex.adapter_fixture_version.endswith(
        "-web-v3" if os.name == "nt" else "-web-v1"
    )

    claude = _versioned_fixture(
        "claude-code",
        "2.1.177",
        role="moderator",
        access_mode="packet-only",
        source_environment={},
        research_mode="live-web",
    )
    claude_template = claude.capability_fixture.template
    assert claude_template["tools"] == ["WebSearch", "WebFetch"]
    assert claude_template["allowed_tools"] == ["WebSearch", "WebFetch"]
    assert claude_template["mcp_servers"] == []
    assert claude_template["setting_sources"] == []
    allowed_index = claude.static_flags.index("--allowedTools")
    assert claude.static_flags[allowed_index : allowed_index + 3] == (
        "--allowedTools",
        "WebSearch",
        "WebFetch",
    )

    grok = _versioned_fixture(
        "grok-build",
        "0.1.220",
        role="participant",
        access_mode="packet-only",
        source_environment={},
        research_mode="live-web",
    )
    grok_template = grok.capability_fixture.template
    assert grok_template["tools"] == ["WebSearch", "WebFetch"]
    assert grok_template["web_search"] is True
    assert grok_template["client_capabilities"] == {}
    assert grok_template["mcp_servers"] == []
    assert grok_template["memory"] is False
    assert grok_template["planning"] is False
    assert grok_template["subagents"] is False


def test_live_web_is_rejected_for_the_writable_driver() -> None:
    with pytest.raises(NativePreflightError, match="writable driver"):
        _versioned_fixture(
            "codex",
            "0.150.0-alpha.12.2",
            role="driver",
            access_mode="driver-write",
            source_environment={},
            research_mode="live-web",
        )


@pytest.mark.asyncio
async def test_native_claude_live_web_probe_names_effective_non_web_surfaces(
    tmp_path: Path,
) -> None:
    executable = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    probe_envelope = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "claude-probe-session",
        "structured_output": {
            "web_search": True,
            "web_fetch": True,
            "filesystem_read": False,
            "filesystem_write": False,
            "shell_command": False,
            "subagent": False,
            "mcp_source": False,
        },
        "modelUsage": {"claude-model": {"output_tokens": 1}},
    }).encode()
    transport = FakeNativeTransport([
        (b"2.1.177 (Claude Code)\n", b"", 0),
        (
            b"--print --output-format --json-schema --resume --safe-mode --tools "
            b"--allowedTools --mcp-config --strict-mcp-config --setting-sources\n",
            b"",
            0,
        ),
        (b'{"loggedIn":true}\n', b"", 0),
        (probe_envelope, b"", 0),
    ])
    target = AgentTarget(runtime="claude-code", model="claude-model", effort=None)
    adapter = ClaudeAdapter(
        target,
        role="participant",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=KnownCredentials(),
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=4096,
        stderr_limit=4096,
        graceful_kill_seconds=1,
        source_environment=_source_environment(),
        transport=transport,
        which=lambda _: str(executable),
        research_mode="live-web",
    )

    await adapter.preflight(target)

    material = adapter.preflight_material()
    assert material.adapter_fixture_version.endswith("-web-v2")
    assert material.fixture.probe_ids == (
        "web-search-allow",
        "web-fetch-allow",
        "filesystem-read-deny",
        "filesystem-write-deny",
        "shell-deny",
        "subagent-deny",
        "mcp-source-deny",
    )
    arguments = transport.calls[-1]["plan"].arguments
    assert arguments[arguments.index("--tools") + 1] == "WebSearch,WebFetch"
    assert arguments[arguments.index("--allowedTools") + 1 :][:2] == (
        "WebSearch",
        "WebFetch",
    )
    prompt = transport.calls[-1]["stdin"].decode("utf-8")
    assert "internal response/finalization control" in prompt
    assert "shell command" in prompt and "subagent" in prompt


@pytest.mark.asyncio
async def test_native_codex_start_structured_and_resume_are_stdin_only(tmp_path: Path) -> None:
    executable = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    events = lambda session, text: (  # noqa: E731
        json.dumps({"type": "thread.started", "thread_id": session})
        + "\n"
        + json.dumps({
            "type": "item.updated",
            "item": {"id": "web-1", "type": "web_search", "query": "Example Domain"},
        })
        + "\n"
        + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        + "\n"
        + json.dumps({"type": "turn.completed", "model": "codex-model", "usage": {"output_tokens": 1}})
        + "\n"
    ).encode()
    transport = FakeNativeTransport([
        (b"codex-cli 0.151.0-alpha.7.1\n", b"", 0),
        (b"--json --output-schema --ignore-user-config --ignore-rules --strict-config --skip-git-repo-check\n", b"", 0),
        (b"SESSION_ID --json --output-schema --ignore-user-config --ignore-rules --strict-config --skip-git-repo-check\n", b"", 0),
        (b"Logged in\n", b"", 0),
        (_codex_doctor_report(), b"", 1),
        (
            events(
                "probe-session",
                json.dumps(
                    {
                        "neutral_read": os.name != "nt",
                        "filesystem_write": False,
                        "network": False,
                        "tool_expansion": False,
                    }
                ),
            ),
            b"",
            0,
        ),
        (events("session-1", '{"answer":"ok"}'), b"", 0),
        (events("session-1", '{"repair":"done"}'), b"", 0),
        (b"native failure\n", b"bounded stderr\n", 7),
        (b"{not-json}\n", b"", 0),
    ])
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    adapter = CodexAdapter(
        target,
        role="reviewer",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=KnownCredentials(),
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=4096,
        stderr_limit=4096,
        graceful_kill_seconds=1,
        source_environment=_source_environment(),
        transport=transport,
        which=lambda _: str(executable),
    )
    await adapter.preflight(target)
    packet_probe_call = transport.calls[-1]
    assert packet_probe_call["plan"].arguments[0] == "exec"
    probe_parent = adapter.store.role_directories_root or adapter.store.state_root
    assert Path(packet_probe_call["cwd"]).is_relative_to(probe_parent)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    concrete = instantiate_capability_template(
        adapter.preflight_material().fixture,
        {"neutral_role_dir": neutral},
    )
    _binding(
        adapter,
        "reviewer-a",
        "review",
        concrete,
        {"neutral_role_dir": neutral},
    )
    adapter._bindings[("reviewer", "reviewer-a", "repair")] = adapter._bindings.pop(  # type: ignore[attr-defined]
        ("reviewer", "reviewer-a", "review")
    )
    adapter._bindings[("reviewer", "reviewer-a", "review")] = adapter._bindings[  # type: ignore[attr-defined]
        ("reviewer", "reviewer-a", "repair")
    ]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}
    repair_schema = {"type": "object", "properties": {"repair": {"type": "string"}}, "required": ["repair"], "additionalProperties": False}
    hostile_prompt = "spaces ' \"\nUnicode 😀 $() ` & | ^ % < >"
    first = await adapter.start(
        _request(neutral, phase="review", schema=schema, prompt=hostile_prompt)
    )
    second = await adapter.resume(
        "session-1", _request(neutral, phase="repair", schema=repair_schema)
    )
    assert first.session_id == second.session_id == "session-1"
    turn_calls = transport.calls[-2:]
    assert [call["stdin"] for call in turn_calls] == [
        hostile_prompt.encode(),
        b"complete prompt over stdin",
    ]
    assert all(hostile_prompt not in " ".join(call["plan"].arguments) for call in turn_calls)
    schema_paths = [
        call["plan"].arguments[call["plan"].arguments.index("--output-schema") + 1]
        for call in turn_calls
    ]
    assert schema_paths == ["output-schema-review.json", "output-schema-repair.json"]
    assert json.loads((neutral / schema_paths[0]).read_text(encoding="utf-8")) == schema
    assert json.loads((neutral / schema_paths[1]).read_text(encoding="utf-8")) == repair_schema
    assert "--sandbox" not in turn_calls[0]["plan"].arguments
    assert "--strict-config" in turn_calls[0]["plan"].arguments
    overrides = [
        turn_calls[0]["plan"].arguments[index + 1]
        for index, value in enumerate(turn_calls[0]["plan"].arguments[:-1])
        if value == "-c"
    ]
    assert 'default_permissions="dialectic-packet"' in overrides
    assert any(value.startswith("permissions=") for value in overrides)
    assert not any(value.startswith("profile=") for value in overrides)
    doctor_calls = [
        call for call in transport.calls
        if call["plan"].arguments[0:2] == ("doctor", "--json")
    ]
    assert len(doctor_calls) == 1
    doctor_arguments = doctor_calls[0]["plan"].arguments
    doctor_overrides = [
        doctor_arguments[index + 1]
        for index, value in enumerate(doctor_arguments[:-1])
        if value == "-c"
    ]
    if os.name == "nt":
        expected_windows_backend = 'windows={"sandbox"="elevated"}'
        assert expected_windows_backend in overrides
        assert expected_windows_backend in doctor_overrides
        assert adapter.fixture.adapter_fixture_version.endswith("-v3")
    else:
        assert not any(value.startswith("windows=") for value in overrides)
        assert not any(value.startswith("windows=") for value in doctor_overrides)
    assert adapter.preflight_material().attestation.managed_policy_sha256 != hashlib.sha256(
        b"{}\n"
    ).hexdigest()
    assert adapter.take_invocation_evidence().process_exit_code == 0  # type: ignore[union-attr]
    with pytest.raises(NativeTurnError):
        await adapter.start(_request(neutral, phase="review"))
    assert adapter.take_invocation_evidence().process_exit_code == 7  # type: ignore[union-attr]
    with pytest.raises(NativeEnvelopeError) as malformed_error:
        await adapter.start(_request(neutral, phase="review"))
    assert bounded_preflight_diagnostic(malformed_error.value) == (
        "invalid Codex JSONL event"
    )
    malformed = adapter.take_invocation_evidence()
    assert malformed is not None and malformed.attempt_end_reason == "agent-failed"

    duplicate_web_id_events = (
        json.dumps({"type": "thread.started", "thread_id": "web-session"})
        + "\n"
        + '{"type":"item.started","item":{"id":"item_1","type":"web_search",'
        '"id":"exec-77714d2b-51e5-46e5-b90c-b063d90ffac6","query":""}}\n'
        + json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"answer":"web ok"}'},
            }
        )
        + "\n"
        + json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}})
        + "\n"
    )
    adapter.research_mode = "live-web"
    parsed_web = adapter._parse_envelope(  # type: ignore[attr-defined]
        duplicate_web_id_events,
        _request(neutral, phase="review", schema=schema),
    )
    assert parsed_web.structured_output == {"answer": "web ok"}
    with pytest.raises(NativeEnvelopeError, match="invalid Codex JSONL event"):
        adapter._parse_envelope(  # type: ignore[attr-defined]
            duplicate_web_id_events.replace(
                '"type":"web_search","id"',
                '"type":"agent_message","id"',
            ),
            _request(neutral, phase="review", schema=schema),
        )
    adapter.research_mode = "offline"
    with pytest.raises(NativeEnvelopeError, match="invalid Codex JSONL event"):
        adapter._parse_envelope(  # type: ignore[attr-defined]
            duplicate_web_id_events,
            _request(neutral, phase="review", schema=schema),
        )

    calls_before_unsafe_resume = len(transport.calls)
    with pytest.raises(NativeEnvelopeError, match="argv grammar"):
        await adapter.resume("bad session", _request(neutral, phase="repair"))
    assert len(transport.calls) == calls_before_unsafe_resume

    original_turn_arguments = adapter._turn_arguments

    def omit_attested_flag(*args, **kwargs):  # type: ignore[no-untyped-def]
        return tuple(
            value
            for value in original_turn_arguments(*args, **kwargs)
            if value != "--strict-config"
        )

    adapter._turn_arguments = omit_attested_flag  # type: ignore[method-assign]
    calls_before_drift = len(transport.calls)
    with pytest.raises(NativePreflightError, match="attested static flag"):
        await adapter.start(_request(neutral, phase="review", schema=schema))
    assert len(transport.calls) == calls_before_drift


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("managed_content", "diagnostic"),
    [
        ('sandbox_mode = "workspace-write"\n', "legacy sandbox"),
        ('[mcp_servers.managed]\ncommand = "forbidden"\n', "enables MCP servers"),
    ],
)
async def test_native_codex_preflight_rejects_displacing_managed_policy(
    tmp_path: Path,
    managed_content: str,
    diagnostic: str,
) -> None:
    executable = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    if os.name == "nt":
        managed = codex_home / "managed_config.toml"
    else:
        pytest.skip("the POSIX managed defaults path is system-owned")
    managed.write_text(managed_content, encoding="utf-8")
    source = _source_environment()
    source["CODEX_HOME"] = str(codex_home)
    transport = FakeNativeTransport([
        (b"codex-cli 0.151.0-alpha.7.1\n", b"", 0),
        (b"--json --output-schema --ignore-user-config --ignore-rules --strict-config --skip-git-repo-check\n", b"", 0),
        (b"SESSION_ID --json --output-schema --ignore-user-config --ignore-rules --strict-config --skip-git-repo-check\n", b"", 0),
        (b"Logged in\n", b"", 0),
        (_codex_doctor_report(), b"", 1),
    ])
    target = AgentTarget(runtime="codex", model="codex-model", effort="high")
    adapter = CodexAdapter(
        target,
        role="reviewer",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=KnownCredentials(),
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=4096,
        stderr_limit=4096,
        graceful_kill_seconds=1,
        source_environment=source,
        transport=transport,
        which=lambda _: str(executable),
    )
    with pytest.raises(NativePreflightError, match=diagnostic):
        await adapter.preflight(target)
    assert len(transport.calls) == 5


@pytest.mark.asyncio
async def test_native_claude_fresh_structured_turn_uses_safe_empty_surfaces(tmp_path: Path) -> None:
    executable = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    envelope = json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "claude-session",
        "structured_output": {"answer": "ok"},
        "modelUsage": {"claude-model": {"output_tokens": 1}},
    }).encode()
    transport = FakeNativeTransport([
        (b"2.1.177 (Claude Code)\n", b"", 0),
        (b"--print --output-format --json-schema --resume --safe-mode --tools --mcp-config --strict-mcp-config --setting-sources\n", b"", 0),
        (b'{"loggedIn":true}\n', b"", 0),
        (envelope, b"", 0),
        (envelope, b"", 0),
    ])
    target = AgentTarget(runtime="claude-code", model="claude-model", effort=None)
    adapter = ClaudeAdapter(
        target,
        role="reviewer",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=KnownCredentials(),
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=4096,
        stderr_limit=4096,
        graceful_kill_seconds=1,
        source_environment=_source_environment(),
        transport=transport,
        probe_provider=recorded_probe_provider,
        which=lambda _: str(executable),
    )
    await adapter.preflight(target)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    concrete = {"profile": "packet"}
    _binding(adapter, "reviewer-a", "review", concrete)
    bound = adapter._bindings[("reviewer", "reviewer-a", "review")]  # type: ignore[attr-defined]
    bound.dynamic_paths["neutral_role_dir"] = neutral  # type: ignore[index]
    adapter._bindings[("reviewer", "reviewer-a", "repair")] = bound  # type: ignore[attr-defined]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    response = await adapter.start(_request(neutral, phase="review", schema=schema))
    arguments = transport.calls[-1]["plan"].arguments
    assert response.structured_output == {"answer": "ok"}
    assert "--safe-mode" in arguments and arguments[arguments.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in arguments and "--setting-sources" in arguments
    resumed = await adapter.resume(
        "claude-session", _request(neutral, phase="repair", schema=schema)
    )
    assert resumed.session_id == "claude-session"
    resume_arguments = transport.calls[-1]["plan"].arguments
    assert resume_arguments[resume_arguments.index("--resume") + 1] == "claude-session"


@pytest.mark.asyncio
async def test_native_claude_capability_probe_preserves_bounded_failure_detail(
    tmp_path: Path,
) -> None:
    executable = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    secret = "test-anthropic-secret"
    credentials = KnownCredentials([KnownCredential("ANTHROPIC_API_KEY", secret)])
    help_output = (
        b"--print --output-format --json-schema --resume --safe-mode --tools "
        b"--mcp-config --strict-mcp-config --setting-sources\n"
    )
    transport = FakeNativeTransport([
        (b"2.1.177 (Claude Code)\n", b"", 0),
        (help_output, b"", 0),
        (b'{"loggedIn":true}\n', b"", 0),
        (
            b"",
            f"API Error: 401 OAuth access token has expired: {secret}".encode(),
            1,
        ),
    ])
    source = _source_environment()
    source["ANTHROPIC_API_KEY"] = secret
    adapter = ClaudeAdapter(
        AgentTarget(runtime="claude-code", model="claude-model", effort=None),
        role="participant",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=credentials,
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=4096,
        stderr_limit=4096,
        graceful_kill_seconds=1,
        source_environment=source,
        transport=transport,
        which=lambda _: str(executable),
    )

    with pytest.raises(NativePreflightError) as rejected:
        await adapter.preflight(adapter.target)

    message = str(rejected.value)
    assert "pinned Claude capability probe process failed" in message
    assert "end_reason=completed, exit_code=1, cleanup_confirmed=True" in message
    assert "401 OAuth access token has expired" in message
    assert secret not in message


@pytest.mark.asyncio
async def test_grok_persistent_participant_has_three_epochs_one_unit_and_once_cleanup(tmp_path: Path) -> None:
    executable = tmp_path / ("grok.exe" if os.name == "nt" else "grok")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    transport = FakeNativeTransport([
        (b"grok 0.1.220\n", b"", 0),
        (b"agent stdio inspect --no-auto-update --no-memory --disable-web-search --no-plan --no-subagents --safe-mode --tools\n", b"", 0),
        (b"agent stdio inspect --no-auto-update --no-memory --disable-web-search --no-plan --no-subagents --safe-mode --tools\n", b"", 0),
        (b"agent stdio inspect --no-auto-update --no-memory --disable-web-search --no-plan --no-subagents --safe-mode --tools\n", b"", 0),
        (b'{"configSources":[],"mcpServers":[],"tools":[]}\n', b"", 0),
    ])
    factory = FakeAcpFactory([
        [],
        ['{"phase":"opening"}', '{"phase":"cross"}', '{"phase":"ballot"}'],
        ['{"phase":"opening"}'],
        ['{"phase":"opening"}'],
        ['{"phase":"opening"}'],
        ['{"phase":"opening"}'],
        ["```json\n{not-json}\n```"],
        [AcpProtocolError("out-of-sequence traffic")],
        [AcpProtocolError("stream overflow")],
        ['{"phase":"opening"}', '{"phase":"cross"}', '{"phase":"ballot"}'],
    ], settings=[
        {}, {}, {}, {}, {}, {}, {}, {}, {"close_data": b"x" * 2048}, {},
    ])
    credentials = KnownCredentials([KnownCredential("XAI_API_KEY", "secretvalue")])
    target = AgentTarget(runtime="grok-build", model="grok-model", effort=None)
    adapter = GrokAdapter(
        target,
        role="participant",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=credentials,
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        graceful_kill_seconds=1,
        source_environment={**_source_environment(), "XAI_API_KEY": "secretvalue"},
        transport=transport,
        probe_provider=recorded_probe_provider,
        which=lambda _: str(executable),
        acp_factory=factory,
    )
    await adapter.preflight(target)
    material = adapter.preflight_material()
    assert material.process_lifecycle == "persistent-acp-session"
    assert material.process_local_continuation is True
    assert material.prompt_transport == "acp-stdio"
    assert {
        "inspect:config-sources=0",
        "inspect:mcp-servers=0",
        "inspect:tools=0",
    }.issubset(material.effective_static_flags)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    concrete = {"profile": "packet"}
    _binding(adapter, "participant-a", "opening", concrete)
    bound = adapter._bindings.pop(("participant", "participant-a", "opening"))  # type: ignore[attr-defined]
    bound.dynamic_paths["neutral_role_dir"] = neutral  # type: ignore[index]
    for phase in ("opening", "cross-examination", "ballot"):
        adapter._bindings[("participant", "participant-a", phase)] = bound  # type: ignore[attr-defined]

    def request(phase: str) -> AgentRequest:
        return AgentRequest(
            role="participant",
            target_id="participant-a",
            turn_phase=phase,
            prompt=phase,
            output_schema={"type": "object", "properties": {"phase": {"type": "string"}}, "required": ["phase"], "additionalProperties": False},
            timeout_seconds=10,
            working_directory=str(neutral),
            access_mode="packet-only",
        )

    opening_request = request("opening")
    cross_request = request("cross-examination")
    ballot_request = request("ballot")
    opening = await adapter.start(opening_request)
    with pytest.raises(NativeEnvelopeError, match="absent or mismatched"):
        await adapter.resume("different-session", cross_request)
    await adapter.prepare_resume(opening.session_id or "", cross_request)
    opening_evidence = adapter.take_invocation_evidence()
    cross = await adapter.resume(opening.session_id or "", cross_request)
    await adapter.prepare_resume(opening.session_id or "", ballot_request)
    cross_evidence = adapter.take_invocation_evidence()
    ballot = await adapter.resume(opening.session_id or "", ballot_request)
    await adapter.close_retained_session(opening.session_id or "", "completed")
    ballot_evidence = adapter.take_invocation_evidence()

    evidences = [opening_evidence, cross_evidence, ballot_evidence]
    assert all(item is not None for item in evidences)
    assert {item.process_unit_id for item in evidences if item} == {factory.leases[1].process_unit_id}
    assert [item.process_origin for item in evidences if item] == [
        "spawned-for-attempt",
        "retained-from-prior-turn",
        "retained-from-prior-turn",
    ]
    assert [item.process_disposition for item in evidences if item] == [
        "retained-for-session",
        "retained-for-session",
        "closed",
    ]
    assert opening.session_id == cross.session_id == ballot.session_id
    assert factory.leases[1].switch_count == 2 and factory.leases[1].close_count == 1
    assert opening_evidence and opening_evidence.stdout.result.discarded_guard_reason == "epoch-boundary"
    assert cross_evidence is not None
    assert b"moderation" in cross_evidence.stdout.persisted
    assert b"secretvalue" not in (
        opening_evidence.stdout.persisted + cross_evidence.stdout.persisted
    )
    with pytest.raises(RuntimeError):
        await adapter.close_retained_session(opening.session_id or "", "completed")

    interrupted = await adapter.start(opening_request)
    await adapter.prepare_resume(interrupted.session_id or "", cross_request)
    assert adapter.take_invocation_evidence() is not None
    await adapter.close_retained_session(interrupted.session_id or "", "phase-failure")
    interrupted_evidence = adapter.take_invocation_evidence()
    assert interrupted_evidence is not None
    assert interrupted_evidence.attempt_end_reason == "peer-failure"
    assert interrupted_evidence.process_origin == "retained-from-prior-turn"
    assert interrupted_evidence.process_disposition == "closed"
    assert factory.leases[2].close_count == 1

    overflowed = await adapter.start(opening_request)
    factory.leases[3].boundary_error = AcpProtocolError("stream overflow")
    factory.leases[3].close_data = b"x" * 2048
    with pytest.raises(NativeTurnError) as overflow:
        await adapter.prepare_resume(overflowed.session_id or "", cross_request)
    assert overflow.value.kind == "AGENT_OUTPUT_TOO_LARGE"
    overflow_evidence = adapter.take_invocation_evidence()
    assert overflow_evidence is not None
    assert overflow_evidence.attempt_end_reason == "output-limit"
    assert overflow_evidence.stdout.result.truncated is True
    assert factory.leases[3].close_count == 1

    exited = await adapter.start(opening_request)
    factory.leases[4].boundary_error = AcpProtocolError("process exited")
    with pytest.raises(NativeEnvelopeError, match="before the next prompt"):
        await adapter.prepare_resume(exited.session_id or "", cross_request)
    exit_evidence = adapter.take_invocation_evidence()
    assert exit_evidence is not None
    assert exit_evidence.attempt_end_reason == "agent-failed"
    assert factory.leases[4].close_count == 1

    unclean = await adapter.start(opening_request)
    factory.leases[5].boundary_error = AcpProtocolError("process exited")
    factory.leases[5].cleanup_confirmed = False
    with pytest.raises(NativeTurnError) as cleanup:
        await adapter.prepare_resume(unclean.session_id or "", cross_request)
    assert cleanup.value.kind == "PROCESS_CLEANUP_FAILED"
    cleanup_evidence = adapter.take_invocation_evidence()
    assert cleanup_evidence is not None
    assert cleanup_evidence.failure_kind == "PROCESS_CLEANUP_FAILED"
    assert cleanup_evidence.process_disposition == "cleanup-failed"
    assert factory.leases[5].close_count == 1

    with pytest.raises(NativeEnvelopeError, match="deterministic validation"):
        await adapter.start(opening_request)
    malformed_evidence = adapter.take_invocation_evidence()
    assert malformed_evidence is not None
    assert malformed_evidence.attempt_end_reason == "agent-failed"
    assert factory.leases[6].close_count == 1

    with pytest.raises(NativeEnvelopeError, match="persistent start failed"):
        await adapter.start(opening_request)
    protocol_evidence = adapter.take_invocation_evidence()
    assert protocol_evidence is not None
    assert protocol_evidence.attempt_end_reason == "agent-failed"
    assert factory.leases[7].close_count == 1

    with pytest.raises(NativeTurnError) as prompt_overflow:
        await adapter.start(opening_request)
    assert prompt_overflow.value.kind == "AGENT_OUTPUT_TOO_LARGE"
    prompt_overflow_evidence = adapter.take_invocation_evidence()
    assert prompt_overflow_evidence is not None
    assert prompt_overflow_evidence.attempt_end_reason == "output-limit"
    assert factory.leases[8].close_count == 1

    final_overflow = await adapter.start(opening_request)
    await adapter.prepare_resume(final_overflow.session_id or "", cross_request)
    assert adapter.take_invocation_evidence() is not None
    await adapter.resume(final_overflow.session_id or "", cross_request)
    await adapter.prepare_resume(final_overflow.session_id or "", ballot_request)
    assert adapter.take_invocation_evidence() is not None
    await adapter.resume(final_overflow.session_id or "", ballot_request)
    factory.leases[9].close_data = b"x" * 2048
    with pytest.raises(NativeTurnError) as final_capture:
        await adapter.close_retained_session(
            final_overflow.session_id or "", "completed"
        )
    assert final_capture.value.kind == "AGENT_OUTPUT_TOO_LARGE"
    final_capture_evidence = adapter.take_invocation_evidence()
    assert final_capture_evidence is not None
    assert final_capture_evidence.attempt_end_reason == "output-limit"
    assert final_capture_evidence.process_disposition == "closed"
    assert factory.leases[9].close_count == 1


@pytest.mark.asyncio
async def test_grok_reviewer_uses_one_ephemeral_acp_unit(tmp_path: Path) -> None:
    executable = tmp_path / ("grok.exe" if os.name == "nt" else "grok")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    transport = FakeNativeTransport([
        (b"grok 0.1.220\n", b"", 0),
        (b"agent stdio inspect --no-auto-update --no-memory --disable-web-search --no-plan --no-subagents --safe-mode --tools\n", b"", 0),
        (b"agent stdio inspect --no-auto-update --no-memory --disable-web-search --no-plan --no-subagents --safe-mode --tools\n", b"", 0),
        (b"agent stdio inspect --no-auto-update --no-memory --disable-web-search --no-plan --no-subagents --safe-mode --tools\n", b"", 0),
        (b'{"configSources":[],"mcpServers":[],"tools":[]}\n', b"", 0),
    ])
    factory = FakeAcpFactory(
        [
            [],
            ['{"answer":"ok"}'],
            [AcpProtocolError("out-of-sequence traffic")],
            [AcpProtocolError("stream overflow")],
            ['{"answer":"ok"}'],
        ],
        settings=[
            {}, {}, {}, {"close_data": b"x" * 2048}, {"close_data": b"x" * 2048}
        ],
    )
    target = AgentTarget(runtime="grok-build", model="grok-model", effort=None)
    adapter = GrokAdapter(
        target,
        role="reviewer",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=KnownCredentials([KnownCredential("XAI_API_KEY", "secretvalue")]),
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        graceful_kill_seconds=1,
        source_environment={**_source_environment(), "XAI_API_KEY": "secretvalue"},
        transport=transport,
        probe_provider=recorded_probe_provider,
        which=lambda _: str(executable),
        acp_factory=factory,
    )
    await adapter.preflight(target)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    concrete = instantiate_capability_template(
        adapter.preflight_material().fixture, {"neutral_role_dir": neutral}
    )
    _binding(
        adapter,
        "reviewer-a",
        "review",
        concrete,
        {"neutral_role_dir": neutral},
    )
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    response = await adapter.start(_request(neutral, phase="review", schema=schema))
    evidence = adapter.take_invocation_evidence()
    assert response.structured_output == {"answer": "ok"}
    assert evidence is not None
    assert evidence.process_lifecycle == "per-turn"
    assert evidence.process_disposition == "closed"
    assert factory.leases[1].close_count == 1
    with pytest.raises(RuntimeError, match="do not support native resume"):
        await adapter.resume("grok-session-2", _request(neutral, phase="review"))
    with pytest.raises(NativeEnvelopeError, match="per-turn prompt failed"):
        await adapter.start(_request(neutral, phase="review", schema=schema))
    assert adapter.take_invocation_evidence() is not None
    assert factory.leases[2].close_count == 1
    with pytest.raises(NativeTurnError) as overflow:
        await adapter.start(_request(neutral, phase="review", schema=schema))
    assert overflow.value.kind == "AGENT_OUTPUT_TOO_LARGE"
    assert adapter.take_invocation_evidence() is not None
    assert factory.leases[3].close_count == 1
    with pytest.raises(NativeTurnError) as final_overflow:
        await adapter.start(_request(neutral, phase="review", schema=schema))
    assert final_overflow.value.kind == "AGENT_OUTPUT_TOO_LARGE"
    final_overflow_evidence = adapter.take_invocation_evidence()
    assert final_overflow_evidence is not None
    assert final_overflow_evidence.attempt_end_reason == "output-limit"
    assert factory.leases[4].close_count == 1


@pytest.mark.parametrize(
    "inventory",
    [
        {"configSources": [], "mcpServers": []},
        {"configSources": ["project"], "mcpServers": [], "tools": []},
        {
            "configSources": [],
            "mcpServers": [],
            "mcp_servers": [],
            "tools": [],
        },
    ],
)
def test_grok_inspect_inventory_must_be_explicit_unique_and_empty(
    tmp_path: Path, inventory: dict[str, object]
) -> None:
    adapter = GrokAdapter(
        AgentTarget(runtime="grok-build", model="grok-model", effort=None),
        role="reviewer",
        access_mode="packet-only",
        store=RunStore(tmp_path / "state"),
        credentials=KnownCredentials(),
        preflight_seconds=10,
        capability_probe_seconds=10,
        stdout_limit=1024,
        stderr_limit=1024,
        graceful_kill_seconds=1,
        source_environment=_source_environment(),
    )
    result = NativeProcessResult(
        process_started=True,
        exit_code=0,
        end_reason="completed",
        failure_kind=None,
        cleanup_confirmed=True,
        stdout=_capture(json.dumps(inventory).encode(), 1024, KnownCredentials()),
        stderr=_capture(b"", 1024, KnownCredentials()),
    )
    with pytest.raises(NativePreflightError):
        adapter._verify_authentication(result)  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recorded_grok_acp_peer_uses_empty_capabilities_and_owned_epochs(
    tmp_path: Path,
) -> None:
    credentials = KnownCredentials([KnownCredential("XAI_API_KEY", "fixture-secret")])
    lease = await ManagedAcpLeaseFactory().open(
        DirectLaunchSpec(
            executable=Path(sys.executable),
            arguments=(str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py"),),
        ),
        cwd=tmp_path,
        environment={**_source_environment(), "XAI_API_KEY": "fixture-secret"},
        model="grok-model",
        process_unit_id="recorded-acp-unit",
        stdout_limit=4096,
        stderr_limit=4096,
        credentials=credentials,
        preflight_seconds=5,
    )
    response = await lease.prompt("hostile ' \" $() ` & | ^ % < > 😀", 5)
    first_epoch = await lease.switch_epoch()
    final_epoch = await lease.close(2)

    assert response.session_id == "recorded-acp-session"
    assert response.text == '{"answer":"ok"}'
    assert response.actual_model == "grok-model"
    assert response.usage == {"output_tokens": 1}
    assert first_epoch.process_exit_code is None
    assert first_epoch.stdout.result.discarded_guard_reason == "epoch-boundary"
    assert final_epoch.process_exit_code == 0
    assert final_epoch.cleanup_confirmed is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recorded_grok_acp_forced_final_shutdown_still_proves_cleanup(
    tmp_path: Path,
) -> None:
    credentials = KnownCredentials([KnownCredential("XAI_API_KEY", "fixture-secret")])
    lease = await ManagedAcpLeaseFactory().open(
        DirectLaunchSpec(
            executable=Path(sys.executable),
            arguments=(
                str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py"),
                "--linger-after-eof",
            ),
        ),
        cwd=tmp_path,
        environment={**_source_environment(), "XAI_API_KEY": "fixture-secret"},
        model="grok-model",
        process_unit_id="forced-acp-unit",
        stdout_limit=4096,
        stderr_limit=4096,
        credentials=credentials,
        preflight_seconds=5,
    )
    await lease.prompt("final turn", 5)
    final_epoch = await lease.close(1)

    assert final_epoch.process_exit_code is not None
    assert final_epoch.cleanup_confirmed is True
    with pytest.raises(AcpProtocolError, match="more than once"):
        await lease.close(1)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recorded_grok_acp_epoch_overflow_closes_with_bounded_evidence(
    tmp_path: Path,
) -> None:
    credentials = KnownCredentials([KnownCredential("XAI_API_KEY", "fixture-secret")])
    lease = await ManagedAcpLeaseFactory().open(
        DirectLaunchSpec(
            executable=Path(sys.executable),
            arguments=(
                str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py"),
                "--overflow-after-prompt",
            ),
        ),
        cwd=tmp_path,
        environment={**_source_environment(), "XAI_API_KEY": "fixture-secret"},
        model="grok-model",
        process_unit_id="overflow-acp-unit",
        stdout_limit=4096,
        stderr_limit=1024,
        credentials=credentials,
        preflight_seconds=5,
    )
    try:
        with pytest.raises(AcpProtocolError, match="exceeded"):
            await lease.prompt("opening", 5)
            await asyncio.sleep(0.05)
            await lease.switch_epoch()
    finally:
        final_epoch = await lease.close(1)

    assert final_epoch.stderr.result.truncated is True
    assert final_epoch.stderr.result.triggered_termination is True
    assert len(final_epoch.stderr.persisted) <= 1024
    assert final_epoch.cleanup_confirmed is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recorded_grok_acp_out_of_sequence_traffic_fails_closed(
    tmp_path: Path,
) -> None:
    credentials = KnownCredentials([KnownCredential("XAI_API_KEY", "fixture-secret")])
    lease = await ManagedAcpLeaseFactory().open(
        DirectLaunchSpec(
            executable=Path(sys.executable),
            arguments=(
                str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py"),
                "--out-of-sequence",
            ),
        ),
        cwd=tmp_path,
        environment={**_source_environment(), "XAI_API_KEY": "fixture-secret"},
        model="grok-model",
        process_unit_id="invalid-acp-unit",
        stdout_limit=4096,
        stderr_limit=4096,
        credentials=credentials,
        preflight_seconds=5,
    )
    try:
        with pytest.raises(AcpProtocolError, match="out of sequence"):
            await lease.prompt("opening", 5)
    finally:
        final_epoch = await lease.close(1)

    assert final_epoch.cleanup_confirmed is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_transport_owns_real_process_and_drains_both_streams(
    tmp_path: Path,
) -> None:
    script = (
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'stderr-ok'); sys.stderr.buffer.flush()"
    )
    result = await BoundedNativeProcessTransport().run(
        DirectLaunchSpec(Path(sys.executable), ("-c", script)),
        cwd=tmp_path,
        environment=dict(os.environ),
        stdin=b"stdout-ok",
        stdout_limit=1024,
        stderr_limit=1024,
        timeout_seconds=10,
        graceful_kill_seconds=2,
        credentials=KnownCredentials(),
    )
    assert result.exit_code == 0
    assert result.end_reason == "completed"
    assert result.cleanup_confirmed
    assert result.stdout.persisted == b"stdout-ok"
    assert result.stderr.persisted == b"stderr-ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_adapters_persist_gate_a_binding_and_real_stream_evidence(
    tmp_path: Path,
    config_data: dict[str, object],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Dialectic Test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    executable = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    executable.write_bytes(b"fixture executable")
    executable.chmod(0o700)
    store = RunStore(
        tmp_path / "state",
        run_id_factory=lambda: "20260829T120000Z-aaaaaaaaaa",
    )

    def adapter(*, role: str, access_mode: str, transport: FakeNativeTransport):
        return CodexAdapter(
            AgentTarget(runtime="codex", model="codex-model", effort="high"),
            role=role,
            access_mode=access_mode,
            store=store,
            credentials=KnownCredentials(),
            preflight_seconds=10,
            capability_probe_seconds=10,
            stdout_limit=65_536,
            stderr_limit=65_536,
            graceful_kill_seconds=1,
            source_environment=_source_environment(),
            transport=transport,
            probe_provider=recorded_probe_provider,
            which=lambda _: str(executable),
        )

    driver = adapter(
        role="driver",
        access_mode="driver-write",
        transport=CodeOnceNativeTransport(driver=True),
    )
    reviewer = adapter(
        role="reviewer",
        access_mode="packet-only",
        transport=CodeOnceNativeTransport(driver=False),
    )
    orchestrator = CodeOnceOrchestrator(
        driver_adapter=driver,
        reviewer_adapters={"same": reviewer},
    )
    service = DialecticService(store, code_executor=orchestrator)
    config_data["reviewers"] = [
        {"id": "same", "target": "@driver", "lens": "correctness"}
    ]
    handle = service.create_run("code")
    record = await service.execute_code_once(
        handle,
        config_bytes=yaml.safe_dump(config_data).encode(),
        task_bytes=b"Create native.txt",
        repository_path=repo,
    )
    assert record.status == "FINALIZED"
    target_evidence = json.loads(
        (handle.path / "audit/targets/driver/driver.json").read_text(encoding="utf-8")
    )
    assert target_evidence["cli_version"] == "0.150.0-alpha.12.2"
    assert target_evidence["resolved_executable"] == str(executable.resolve())
    attempt_path = handle.path / "turns/driver/driver/initial.attempt.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    stdout = (handle.path / "turns/driver/driver/initial.stdout.txt").read_bytes()
    assert attempt["process_origin"] == "spawned-for-attempt"
    assert attempt["process_disposition"] == "closed"
    assert attempt["process_exit_code"] == 0
    assert attempt["stdout"]["persisted_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert not list(handle.path.rglob("*.response.json"))
