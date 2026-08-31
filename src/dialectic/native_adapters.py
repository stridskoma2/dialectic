"""Version-labeled native Codex and Claude Code adapters.

Grok ACP uses the same preflight/evidence contract and extends this base from
``grok_acp.py`` because its fixture-qualified council lifecycle is persistent.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from .adapters import AgentProcessError, ModelMismatchError, verify_model_equivalence
from .capabilities import (
    CapabilityFixture,
    dynamic_path_key,
    instantiate_capability_template,
    validate_cached_attestation,
)
from .contracts import (
    ARTIFACT_SCHEMA_VERSION,
    TOOL_VERSION,
    SessionCloseReason,
)
from .filesystem import stable_filesystem_identity
from .launcher import (
    DirectLaunchSpec,
    LaunchPlanError,
    LaunchSpec,
    WindowsBatchLaunchSpec,
    build_launch_spec,
    resolve_executable,
    validate_argv,
)
from .json_schema import JsonSchemaError, validate_json_schema
from .native_process import (
    BoundedNativeProcessTransport,
    NativeLaunchError,
    NativeProcessResult,
    NativeProcessTransport,
)
from .output import OutputError, extract_json_payload, strict_json_loads
from .redaction import BoundedStreamCapture, CapturedStream, KnownCredentials
from .schemas import (
    AgentRequest,
    AgentResponse,
    AgentTarget,
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    CapabilityProbeResult,
    PreflightResult,
)
from .store import RunStore, canonical_json_bytes

Role = Literal["driver", "reviewer", "participant", "moderator"]
AccessMode = Literal["driver-write", "packet-only"]
ProcessLifecycle = Literal["per-turn", "persistent-acp-session"]
ProbeProvider = Callable[
    ["NativeAdapterBase", "NativeAdapterFixture", Mapping[str, str]],
    Awaitable[Sequence[CapabilityProbeResult]] | Sequence[CapabilityProbeResult],
]

_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CODEX_VERSION_RE = re.compile(r"^codex-cli (?P<version>\S+)$")
_CLAUDE_VERSION_RE = re.compile(r"^(?P<version>\S+) \(Claude Code\)$")


class NativePreflightError(RuntimeError):
    pass


class NativeEnvelopeError(RuntimeError):
    pass


class NativeTurnError(RuntimeError):
    def __init__(self, kind: str | None, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class NativeAdapterFixture:
    runtime: Literal["codex", "claude-code", "grok-build"]
    cli_version: str
    executable_name: str
    adapter_fixture_version: str
    fixture_test_version: str
    credential_environment_names: tuple[str, ...]
    required_non_secret_environment_names: tuple[str, ...]
    optional_non_secret_environment_names: tuple[str, ...]
    saved_auth_paths: tuple[Path, ...]
    static_flags: tuple[str, ...]
    prompt_transport: Literal["stdin", "acp-stdio"]
    process_lifecycle: ProcessLifecycle
    process_local_continuation: bool
    capability_fixture: CapabilityFixture


@dataclass(frozen=True, slots=True)
class NativePreflightMaterial:
    launch_plan: LaunchSpec
    resolved_executable: Path
    resolved_executable_identity: str
    resolved_executable_sha256: str
    spawned_root_executable: Path
    spawned_root_identity: str
    spawned_root_sha256: str
    cli_version: str
    effective_static_flags: tuple[str, ...]
    trusted_environment: Mapping[str, str]
    credential_environment_names: tuple[str, ...]
    denied_credential_path_sha256s: tuple[str, ...]
    fixture: CapabilityFixture
    adapter_fixture_version: str
    prompt_transport: Literal["stdin", "acp-stdio"]
    process_lifecycle: ProcessLifecycle
    process_local_continuation: bool
    attestation: CapabilityAttestationArtifact


@dataclass(frozen=True, slots=True)
class NativeInvocationEvidence:
    started_at: datetime
    response_completed_at: datetime | None
    capture_completed_at: datetime
    process_origin: Literal["none", "spawned-for-attempt", "retained-from-prior-turn"]
    process_lifecycle: ProcessLifecycle
    process_unit_id: str | None
    process_exit_code: int | None
    attempt_end_reason: str
    failure_kind: str | None
    process_disposition: Literal[
        "not-started", "retained-for-session", "closed", "cleanup-failed"
    ]
    stdout: CapturedStream
    stderr: CapturedStream
    bounded_diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class _BoundProfile:
    binding: CapabilityBindingArtifact
    concrete_profile: Mapping[str, Any]
    dynamic_paths: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class _ParsedEnvelope:
    session_id: str | None
    text: str
    structured_output: dict[str, Any] | None
    actual_model: str | None
    usage: dict[str, Any] | None


class NativeAdapterBase:
    runtime: Literal["codex", "claude-code", "grok-build"]

    def __init__(
        self,
        target: AgentTarget,
        *,
        role: Role,
        access_mode: AccessMode,
        store: RunStore,
        credentials: KnownCredentials,
        preflight_seconds: float,
        capability_probe_seconds: float,
        stdout_limit: int,
        stderr_limit: int,
        graceful_kill_seconds: float,
        source_environment: Mapping[str, str] | None = None,
        transport: NativeProcessTransport | None = None,
        probe_provider: ProbeProvider | None = None,
        canonical_aliases: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        if target.runtime != self.runtime:
            raise ValueError(f"{type(self).__name__} cannot serve {target.runtime}")
        if access_mode == "driver-write" and role != "driver":
            raise ValueError("driver-write access belongs only to the driver role")
        self.target = target
        self.role = role
        self.access_mode = access_mode
        self.store = store
        self.credentials = credentials
        self.preflight_seconds = preflight_seconds
        self.capability_probe_seconds = capability_probe_seconds
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self.graceful_kill_seconds = graceful_kill_seconds
        self.source_environment = dict(
            os.environ if source_environment is None else source_environment
        )
        self.transport = transport or BoundedNativeProcessTransport()
        self.probe_provider = probe_provider
        self.canonical_aliases = dict(canonical_aliases or {})
        self.which = which
        self._material: NativePreflightMaterial | None = None
        self._fixture: NativeAdapterFixture | None = None
        self._bindings: dict[tuple[str, str, str], _BoundProfile] = {}
        self._last_invocation: NativeInvocationEvidence | None = None

    @property
    def fixture(self) -> NativeAdapterFixture:
        if self._fixture is None:
            raise NativePreflightError("versioned adapter fixture is unavailable before preflight")
        return self._fixture

    @property
    def process_local_continuation(self) -> bool:
        """Whether the verified fixture permits a process-local session lease."""
        return self.fixture.process_local_continuation

    @property
    def prompt_transport(self) -> Literal["stdin", "acp-stdio"]:
        """The transport selected by the verified native fixture."""
        return self.fixture.prompt_transport

    async def preflight(self, target: AgentTarget) -> PreflightResult:
        if target != self.target:
            raise NativePreflightError("native adapter target mismatch")
        bootstrap = _bootstrap_fixture(self.runtime)
        try:
            base_plan = resolve_executable(
                bootstrap.executable_name, (), which=self.which
            )
        except LaunchPlanError as exc:
            raise NativePreflightError(str(exc)) from exc
        resolved = _resolved_cli(base_plan).resolve(strict=True)
        spawned_root = _spawned_root(base_plan).resolve(strict=True)
        preliminary_environment, _ = _trusted_environment(
            bootstrap, self.source_environment, self.credentials
        )
        version_result = await self._preflight_command(
            resolved, self._version_arguments(), preliminary_environment
        )
        version = self._parse_version(
            _strict_utf8(version_result.stdout.persisted, "version output")
        )
        fixture = _versioned_fixture(
            self.runtime,
            version,
            role=self.role,
            access_mode=self.access_mode,
            source_environment=self.source_environment,
        )
        if isinstance(base_plan, WindowsBatchLaunchSpec) and self.runtime in {
            "codex",
            "claude-code",
        }:
            raise NativePreflightError(
                f"the pinned {self.runtime} fixture cannot safely encode its structured "
                "turn controls through a Windows batch shim"
            )
        _validate_effort(self.target, fixture)
        self._fixture = fixture
        environment, credential_names = _trusted_environment(
            fixture, self.source_environment, self.credentials
        )
        help_text = ""
        for arguments in self._help_arguments():
            result = await self._preflight_command(resolved, arguments, environment)
            help_text += _strict_utf8(
                result.stdout.persisted + result.stderr.persisted, "help output"
            )
        self._verify_help(help_text)
        auth = await self._preflight_command(
            resolved, self._authentication_arguments(), environment
        )
        self._verify_authentication(auth)
        await self._runtime_preflight(resolved, environment)

        executable_sha = _sha256_file(resolved)
        spawned_sha = _sha256_file(spawned_root)
        expected = {
            "runtime": self.runtime,
            "executable_identity": stable_filesystem_identity(resolved),
            "executable_sha256": executable_sha,
            "spawned_root_identity": stable_filesystem_identity(spawned_root),
            "spawned_root_sha256": spawned_sha,
            "cli_version": version,
            "platform_backend": _platform_backend(),
            "elevation_state": _elevation_state(),
            "adapter_fixture_version": fixture.adapter_fixture_version,
            "fixture_test_version": fixture.fixture_test_version,
            "managed_policy_sha256": await self._managed_policy_fingerprint(
                resolved, environment, fixture
            ),
        }
        cache_key = _canonical_hash(
            {**expected, "profile_template_sha256": fixture.capability_fixture.template_sha256}
        )
        attestation = self._cached_attestation(
            cache_key, fixture.capability_fixture, expected
        )
        if attestation is None:
            attestation = await asyncio.wait_for(
                self._run_capability_probe(fixture, expected),
                timeout=self.capability_probe_seconds,
            )
            attestation = validate_cached_attestation(
                canonical_json_bytes(attestation),
                fixture=fixture.capability_fixture,
                expected_fields=expected,
            )
            self.store.write_capability_attestation(cache_key, attestation)

        self._material = NativePreflightMaterial(
            launch_plan=base_plan,
            resolved_executable=resolved,
            resolved_executable_identity=expected["executable_identity"],
            resolved_executable_sha256=executable_sha,
            spawned_root_executable=spawned_root,
            spawned_root_identity=expected["spawned_root_identity"],
            spawned_root_sha256=spawned_sha,
            cli_version=version,
            effective_static_flags=(
                *fixture.static_flags,
                *self._verified_inventory_flags(),
            ),
            trusted_environment=environment,
            credential_environment_names=credential_names,
            denied_credential_path_sha256s=tuple(
                sorted(
                    hashlib.sha256(_canonical_path(path).encode("utf-8")).hexdigest()
                    for path in fixture.saved_auth_paths
                )
            ),
            fixture=fixture.capability_fixture,
            adapter_fixture_version=fixture.adapter_fixture_version,
            prompt_transport=fixture.prompt_transport,
            process_lifecycle=fixture.process_lifecycle,
            process_local_continuation=fixture.process_local_continuation,
            attestation=attestation,
        )
        return PreflightResult(
            target=target,
            requested_model=target.model,
            resolved_requested_model=self.canonical_aliases.get(target.model, target.model),
            actual_model=None,
            authentication_verified=True,
        )

    def preflight_material(self) -> NativePreflightMaterial:
        if self._material is None:
            raise NativePreflightError("native preflight material is unavailable")
        return self._material

    def bind_capability(
        self,
        binding: CapabilityBindingArtifact,
        concrete_profile: Mapping[str, Any],
        dynamic_paths: Mapping[str, Path],
    ) -> None:
        if binding.access_mode != self.access_mode:
            raise NativePreflightError("binding access mode mismatches adapter")
        if binding.concrete_profile_sha256 != _canonical_hash(concrete_profile):
            raise NativePreflightError("binding profile hash mismatches adapter construction")
        phase = binding.binding_id.rsplit(":", 1)[-1]
        self._bindings[(binding.role, binding.target_id, phase)] = _BoundProfile(
            binding, dict(concrete_profile), dict(dynamic_paths)
        )

    def take_invocation_evidence(self) -> NativeInvocationEvidence | None:
        evidence = self._last_invocation
        self._last_invocation = None
        return evidence

    async def start(self, request: AgentRequest) -> AgentResponse:
        return await self._invoke("start", None, request)

    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse:
        if not _SESSION_RE.fullmatch(session_id):
            raise NativeEnvelopeError("native session id violates the argv grammar")
        return await self._invoke("resume", session_id, request)

    async def close_retained_session(
        self, session_id: str, reason: SessionCloseReason
    ) -> None:
        raise RuntimeError("per-turn native adapter has no retained session lease")

    async def _invoke(
        self, operation: Literal["start", "resume"],
        session_id: str | None, request: AgentRequest,
    ) -> AgentResponse:
        if request.access_mode != self.access_mode:
            raise NativePreflightError("request access mode mismatches adapter")
        bound = self._bound_profile(request)
        arguments = self._turn_arguments(operation, session_id, request, bound)
        validate_argv(arguments)
        material = self.preflight_material()
        self._revalidate_material(material)
        plan = build_launch_spec(material.resolved_executable, arguments)
        environment = self._turn_environment(material.trusted_environment, bound)
        started = datetime.now(UTC)
        unit_id = _new_process_unit_id()
        cancellation = asyncio.Event()
        task = asyncio.create_task(
            self.transport.run(
                plan, cwd=Path(request.working_directory), environment=environment,
                stdin=request.prompt.encode("utf-8"),
                stdout_limit=self.stdout_limit, stderr_limit=self.stderr_limit,
                timeout_seconds=request.timeout_seconds,
                graceful_kill_seconds=self.graceful_kill_seconds,
                credentials=self.credentials, cancellation=cancellation,
            )
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation.set()
            try:
                result = await asyncio.shield(task)
                self._set_evidence(started, unit_id, result, "cancelled", None)
            except BaseException as exc:
                self._record_launch_failure(started, exc)
            raise
        except NativeLaunchError as exc:
            self._record_launch_failure(started, exc)
            raise
        end_reason, failure_kind = _result_classification(result)
        self._set_evidence(started, unit_id, result, end_reason, failure_kind)
        if not result.cleanup_confirmed:
            raise NativeTurnError(
                "PROCESS_CLEANUP_FAILED", "native process-unit cleanup failed"
            )
        if end_reason != "response-returned":
            raise NativeTurnError(failure_kind, f"native turn ended as {end_reason}")
        if result.exit_code != 0:
            raise AgentProcessError(
                result.exit_code if result.exit_code is not None else -1,
                "native agent returned a nonzero exit code",
            )
        try:
            parsed = self._parse_envelope(
                _strict_utf8(result.stdout.persisted, "native stdout"), request
            )
            verify_model_equivalence(
                requested=self.target.model,
                resolved=self.canonical_aliases.get(self.target.model),
                actual=parsed.actual_model,
                aliases=self.canonical_aliases,
            )
        except (NativeEnvelopeError, ModelMismatchError):
            self._set_evidence(started, unit_id, result, "agent-failed", None)
            raise
        response_completed = datetime.now(UTC)
        response = AgentResponse(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            runtime=self.runtime,
            requested_model=self.target.model,
            resolved_requested_model=self.canonical_aliases.get(
                self.target.model, self.target.model
            ),
            actual_model=parsed.actual_model,
            session_id=parsed.session_id,
            text=parsed.text,
            structured_output=parsed.structured_output,
            usage=parsed.usage,
        )
        self._last_invocation = NativeInvocationEvidence(
            started_at=started,
            response_completed_at=response_completed,
            capture_completed_at=max(response_completed, datetime.now(UTC)),
            process_origin="spawned-for-attempt",
            process_lifecycle="per-turn",
            process_unit_id=unit_id,
            process_exit_code=result.exit_code,
            attempt_end_reason="response-returned",
            failure_kind=None,
            process_disposition="closed",
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return response

    def _cached_attestation(
        self, cache_key: str, fixture: CapabilityFixture,
        expected: Mapping[str, str],
    ) -> CapabilityAttestationArtifact | None:
        cached = self.store.read_capability_attestation(cache_key)
        if cached is None:
            return None
        try:
            return validate_cached_attestation(
                cached, fixture=fixture, expected_fields=expected
            )
        except Exception:
            return None

    async def _run_capability_probe(
        self, fixture: NativeAdapterFixture, expected: Mapping[str, str]
    ) -> CapabilityAttestationArtifact:
        if self.probe_provider is None:
            results = list(await self._native_capability_probe(fixture))
        else:
            supplied = self.probe_provider(self, fixture, expected)
            results = list(await supplied if inspect.isawaitable(supplied) else supplied)
        if [item.probe_id for item in results] != list(
            fixture.capability_fixture.probe_ids
        ):
            raise NativePreflightError("capability probe returned the wrong closed probe set")
        return CapabilityAttestationArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            **expected,
            profile_template_sha256=fixture.capability_fixture.template_sha256,
            probe_results=results,
            probe_results_sha256=_canonical_hash(
                [item.model_dump(mode="json") for item in results]
            ),
        )

    async def _native_capability_probe(
        self, fixture: NativeAdapterFixture
    ) -> Sequence[CapabilityProbeResult]:
        raise NativePreflightError(
            f"no pinned capability probe is available for {self.runtime} "
            f"{fixture.cli_version} on {_platform_backend()}"
        )

    async def _preflight_command(
        self, executable: Path, arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> NativeProcessResult:
        result = await self.transport.run(
            build_launch_spec(executable, arguments),
            cwd=Path(tempfile.gettempdir()).resolve(strict=True),
            environment=environment, stdin=b"",
            stdout_limit=self.stdout_limit, stderr_limit=self.stderr_limit,
            timeout_seconds=self.preflight_seconds,
            graceful_kill_seconds=self.graceful_kill_seconds,
            credentials=self.credentials,
        )
        if (
            not result.cleanup_confirmed
            or result.end_reason != "completed"
            or result.exit_code != 0
        ):
            raise NativePreflightError(
                f"{self.runtime} preflight command failed: {' '.join(arguments[:2])}"
            )
        return result

    def _bound_profile(self, request: AgentRequest) -> _BoundProfile:
        exact = self._bindings.get((request.role, request.target_id, request.turn_phase))
        if exact is not None:
            return exact
        matches = [
            value for (role, target_id, _), value in self._bindings.items()
            if role == request.role and target_id == request.target_id
        ]
        if len(matches) != 1:
            raise NativePreflightError("no unique Gate B binding authorizes this native turn")
        return matches[0]

    def _turn_environment(
        self, base: Mapping[str, str], bound: _BoundProfile
    ) -> dict[str, str]:
        environment = dict(base)
        temporary = bound.dynamic_paths.get("turn_scratch_tmp") or bound.dynamic_paths.get(
            "neutral_role_dir"
        )
        if temporary is None:
            raise NativePreflightError("binding lacks a controller-owned temporary directory")
        for name in ("TMP", "TEMP", "TMPDIR"):
            environment[name] = str(temporary)
        return environment

    def _revalidate_material(self, material: NativePreflightMaterial) -> None:
        try:
            resolved = material.resolved_executable.resolve(strict=True)
            spawned = material.spawned_root_executable.resolve(strict=True)
        except OSError as exc:
            raise NativePreflightError("recorded native launch path is no longer available") from exc
        if (
            stable_filesystem_identity(resolved) != material.resolved_executable_identity
            or _sha256_file(resolved) != material.resolved_executable_sha256
            or stable_filesystem_identity(spawned) != material.spawned_root_identity
            or _sha256_file(spawned) != material.spawned_root_sha256
        ):
            raise NativePreflightError("recorded native launch identity changed after Gate A")

    def _set_evidence(
        self, started: datetime, unit_id: str, result: NativeProcessResult,
        end_reason: str, failure_kind: str | None,
    ) -> None:
        cleanup_failed = not result.cleanup_confirmed
        self._last_invocation = NativeInvocationEvidence(
            started_at=started,
            response_completed_at=None,
            capture_completed_at=datetime.now(UTC),
            process_origin="spawned-for-attempt",
            process_lifecycle="per-turn",
            process_unit_id=unit_id,
            process_exit_code=result.exit_code,
            attempt_end_reason="cleanup-failed" if cleanup_failed else end_reason,
            failure_kind=(
                "PROCESS_CLEANUP_FAILED" if cleanup_failed else failure_kind
            ),
            process_disposition="cleanup-failed" if cleanup_failed else "closed",
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _record_launch_failure(self, started: datetime, error: BaseException) -> None:
        self._last_invocation = NativeInvocationEvidence(
            started_at=started,
            response_completed_at=None,
            capture_completed_at=datetime.now(UTC),
            process_origin="none",
            process_lifecycle=self.preflight_material().process_lifecycle,
            process_unit_id=None,
            process_exit_code=None,
            attempt_end_reason="launch-failed",
            failure_kind=None,
            process_disposition="not-started",
            stdout=_empty_capture(self.stdout_limit, self.credentials),
            stderr=_empty_capture(self.stderr_limit, self.credentials),
            bounded_diagnostic=f"native launch failed: {type(error).__name__}",
        )

    async def _runtime_preflight(
        self, executable: Path, environment: Mapping[str, str]
    ) -> None:
        return None

    async def _managed_policy_fingerprint(
        self,
        executable: Path,
        environment: Mapping[str, str],
        fixture: NativeAdapterFixture,
    ) -> str:
        del executable, environment, fixture
        return _canonical_hash({"managed_policy": "not-applicable"})

    def _verified_inventory_flags(self) -> tuple[str, ...]:
        return ()

    def _version_arguments(self) -> tuple[str, ...]:
        raise NotImplementedError

    def _help_arguments(self) -> tuple[tuple[str, ...], ...]:
        raise NotImplementedError

    def _authentication_arguments(self) -> tuple[str, ...]:
        raise NotImplementedError

    def _parse_version(self, output: str) -> str:
        raise NotImplementedError

    def _verify_help(self, output: str) -> None:
        raise NotImplementedError

    def _verify_authentication(self, result: NativeProcessResult) -> None:
        raise NotImplementedError

    def _turn_arguments(
        self, operation: Literal["start", "resume"], session_id: str | None,
        request: AgentRequest, bound: _BoundProfile,
    ) -> tuple[str, ...]:
        raise NotImplementedError

    def _parse_envelope(self, stdout: str, request: AgentRequest) -> _ParsedEnvelope:
        raise NotImplementedError


class CodexAdapter(NativeAdapterBase):
    runtime = "codex"

    def _version_arguments(self) -> tuple[str, ...]:
        return ("--version",)

    def _help_arguments(self) -> tuple[tuple[str, ...], ...]:
        return (("exec", "--help"), ("exec", "resume", "--help"))

    def _authentication_arguments(self) -> tuple[str, ...]:
        return ("login", "status")

    def _parse_version(self, output: str) -> str:
        match = _CODEX_VERSION_RE.fullmatch(output.strip())
        if match is None:
            raise NativePreflightError("unrecognized Codex version output")
        return match.group("version")

    def _verify_help(self, output: str) -> None:
        required = (
            "--json", "--output-schema", "--ignore-user-config",
            "--ignore-rules", "--skip-git-repo-check", "--strict-config",
            "SESSION_ID",
        )
        if any(value not in output for value in required):
            raise NativePreflightError("installed Codex CLI lacks a required exec control")

    def _verify_authentication(self, result: NativeProcessResult) -> None:
        text = _strict_utf8(
            result.stdout.persisted + result.stderr.persisted, "Codex auth status"
        )
        if "Logged in" not in text:
            raise NativePreflightError("Codex authentication is unavailable")

    async def _managed_policy_fingerprint(
        self,
        executable: Path,
        environment: Mapping[str, str],
        fixture: NativeAdapterFixture,
    ) -> str:
        with tempfile.TemporaryDirectory(prefix="dialectic-codex-policy-") as root:
            policy_root = Path(root).resolve(strict=True)
            dynamic_paths: dict[str, Path] = {}
            for role in fixture.capability_fixture.dynamic_roles:
                path = policy_root / role
                path.mkdir()
                dynamic_paths[role] = path
            concrete = instantiate_capability_template(
                fixture.capability_fixture, dynamic_paths
            )
            result = await self.transport.run(
                build_launch_spec(
                    executable,
                    ("doctor", "--json", *_codex_overrides(concrete)),
                ),
                cwd=policy_root,
                environment=environment,
                stdin=b"",
                stdout_limit=self.stdout_limit,
                stderr_limit=self.stderr_limit,
                timeout_seconds=self.preflight_seconds,
                graceful_kill_seconds=self.graceful_kill_seconds,
                credentials=self.credentials,
            )
        if not result.cleanup_confirmed or result.end_reason != "completed":
            raise NativePreflightError("Codex effective-policy inspection failed")
        try:
            report = strict_json_loads(
                _strict_utf8(result.stdout.persisted, "Codex doctor output")
            )
        except (OutputError, NativeEnvelopeError) as exc:
            raise NativePreflightError("Codex doctor output is invalid") from exc
        snapshot = _codex_effective_policy_snapshot(
            report,
            expected_version=fixture.cli_version,
            profile_name=str(concrete["default_permissions"]),
            driver=self.access_mode == "driver-write",
        )
        sources = _codex_managed_policy_sources(
            fixture,
            source_environment=self.source_environment,
            profile_name=str(concrete["default_permissions"]),
        )
        return _canonical_hash({"effective": snapshot, "sources": sources})

    def _turn_arguments(
        self, operation: Literal["start", "resume"], session_id: str | None,
        request: AgentRequest, bound: _BoundProfile,
    ) -> tuple[str, ...]:
        common = [
            "--ignore-user-config", "--ignore-rules", "--strict-config", "--json",
            "--model", self.target.model,
        ]
        if self.access_mode == "packet-only":
            common.append("--skip-git-repo-check")
        if self.target.effort is not None:
            common.extend(("-c", f"model_reasoning_effort={json.dumps(self.target.effort)}"))
        common.extend(_codex_overrides(bound.concrete_profile))
        schema_file = _write_schema_file(request, bound)
        if schema_file is not None:
            common.extend(("--output-schema", schema_file))
        if operation == "resume":
            assert session_id is not None
            return ("exec", "resume", *common, session_id, "-")
        return ("exec", *common, "-")

    def _parse_envelope(self, stdout: str, request: AgentRequest) -> _ParsedEnvelope:
        session_id: str | None = None
        text: str | None = None
        actual_model: str | None = None
        usage: dict[str, Any] | None = None
        complete = False
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = strict_json_loads(line)
            except OutputError as exc:
                raise NativeEnvelopeError("invalid Codex JSONL event") from exc
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise NativeEnvelopeError("invalid Codex JSONL event")
            event_type = event["type"]
            if event_type == "thread.started":
                candidate = event.get("thread_id")
                if not isinstance(candidate, str) or not _SESSION_RE.fullmatch(candidate):
                    raise NativeEnvelopeError("Codex thread event lacks a valid session id")
                if session_id is not None and session_id != candidate:
                    raise NativeEnvelopeError("Codex envelope changed its session id")
                session_id = candidate
            elif event_type == "item.completed":
                item = event.get("item")
                if not isinstance(item, dict):
                    raise NativeEnvelopeError("Codex item event is malformed")
                if item.get("type") == "agent_message":
                    candidate = item.get("text")
                    if not isinstance(candidate, str):
                        raise NativeEnvelopeError("Codex agent message lacks text")
                    text = candidate
            elif event_type == "turn.completed":
                raw_usage = event.get("usage")
                if raw_usage is not None and not isinstance(raw_usage, dict):
                    raise NativeEnvelopeError("Codex usage is not an object")
                usage = raw_usage
                model = event.get("model")
                if model is not None and not isinstance(model, str):
                    raise NativeEnvelopeError("Codex actual model is invalid")
                actual_model = model
                complete = True
            elif event_type in {"turn.started", "item.started"}:
                continue
            elif event_type in {"error", "turn.failed"}:
                raise NativeEnvelopeError("Codex envelope reports an error")
            else:
                raise NativeEnvelopeError(f"unrecognized Codex event {event_type}")
        if not complete or text is None:
            raise NativeEnvelopeError("Codex envelope is incomplete")
        structured: dict[str, Any] | None = None
        if request.output_schema is not None:
            try:
                payload = extract_json_payload(text)
                if not isinstance(payload, dict):
                    raise NativeEnvelopeError("Codex structured response is not an object")
                validate_json_schema(payload, request.output_schema)
                structured = payload
                text = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            except (OutputError, JsonSchemaError) as exc:
                raise NativeEnvelopeError("Codex structured response failed validation") from exc
        return _ParsedEnvelope(session_id, text, structured, actual_model, usage)

    async def _native_capability_probe(
        self, fixture: NativeAdapterFixture
    ) -> Sequence[CapabilityProbeResult]:
        if self.access_mode == "driver-write":
            return await self._probe_driver_capabilities(fixture)
        return await self._probe_packet_capabilities(fixture)

    async def _probe_packet_capabilities(
        self, fixture: NativeAdapterFixture
    ) -> Sequence[CapabilityProbeResult]:
        # Use the same private root as real packet roles. The Windows profile
        # explicitly denies the OS temp root, and the elevated sandbox cannot
        # bootstrap a session from virtualized per-user AppData paths.
        with self.store.temporary_role_directory(
            prefix="dialectic-codex-packet-probe-"
        ) as neutral:
            source = neutral / "probe-input.txt"
            source.write_text("dialectic packet probe\n", encoding="utf-8")
            schema = _boolean_object_schema(
                "neutral_read", "filesystem_write", "network", "tool_expansion"
            )
            prompt = (
                "This is a bounded permission probe. Use model-generated child commands to "
                f"read {source}, attempt to create {neutral / 'forbidden.txt'}, attempt a "
                "short outbound HTTPS request, and attempt to request or obtain broader tool "
                "permissions. Finish those commands before emitting any response. Set "
                "neutral_read=true only if the read command prints exactly 'dialectic packet "
                "probe'. Return booleans saying whether each operation succeeded, using the "
                "exact output schema. Do not infer success from the request text."
            )
            parsed = await self._run_probe_turn(
                fixture,
                dynamic_paths={"neutral_role_dir": neutral},
                working_directory=neutral,
                prompt=prompt,
                schema=schema,
                target_id="capability-probe",
            )
            observed = {
                **(
                    {
                        "neutral-cwd-enter-allow": True,
                        "filesystem-read-deny": parsed.get("neutral_read") is False,
                    }
                    if os.name == "nt"
                    else {"neutral-cwd-read-allow": parsed.get("neutral_read") is True}
                ),
                "filesystem-write-deny": (
                    parsed.get("filesystem_write") is False
                    and not (neutral / "forbidden.txt").exists()
                ),
                "network-deny": parsed.get("network") is False,
                "tool-expansion-deny": parsed.get("tool_expansion") is False,
            }
            return _probe_results(fixture, observed, "pinned native Codex packet probe")

    async def _probe_driver_capabilities(
        self, fixture: NativeAdapterFixture
    ) -> Sequence[CapabilityProbeResult]:
        with tempfile.TemporaryDirectory(prefix="dialectic-codex-driver-probe-") as root:
            probe_root = Path(root).resolve(strict=True)
            worktree = probe_root / "worktree"
            original = probe_root / "original"
            state_root = probe_root / "state"
            outside = probe_root / "outside"
            for directory in (original, state_root, outside):
                directory.mkdir()
            _initialize_probe_repository(original)
            _create_probe_linked_worktree(original, worktree)
            git_common = original / ".git"
            scratch_root = worktree / ".dialectic-turn"
            control = scratch_root / "control"
            temporary = scratch_root / "tmp"
            control.mkdir(parents=True)
            temporary.mkdir()

            sentinels = {
                "git": git_common / "dialectic-sentinel.txt",
                "original": original / "dialectic-sentinel.txt",
                "state": state_root / "dialectic-sentinel.txt",
                "outside": outside / "dialectic-sentinel.txt",
                "control": control / "dialectic-sentinel.txt",
            }
            expected_contents: dict[str, bytes] = {}
            for name, path in sentinels.items():
                content = f"{name}-{secrets.token_hex(16)}\n".encode()
                path.write_bytes(content)
                expected_contents[name] = content

            schema = _boolean_object_schema(
                "product_write", "tmp_write", "git_read", "control_write",
                "git_metadata_write", "original_read", "saved_auth_or_state_read",
                "os_temp_read", "outside_read", "network", "permission_expansion",
                "git_hardlink", "original_hardlink", "outside_hardlink",
                "auth_hardlink",
            )
            hardlinks = {
                "git_hardlink": (sentinels["git"], worktree / "git-link.txt"),
                "original_hardlink": (
                    sentinels["original"], worktree / "original-link.txt"
                ),
                "outside_hardlink": (
                    sentinels["outside"], temporary / "outside-link.txt"
                ),
                "auth_hardlink": (sentinels["state"], temporary / "auth-link.txt"),
            }
            prompt = (
                "This is a bounded native permission probe. Use child commands and return only "
                "the exact boolean schema. Report true only when the named operation actually "
                "succeeds. Test: create product-probe.txt in the worktree; create tmp-probe.txt "
                "under .dialectic-turn/tmp; run read-only git status and base-object inspection; "
                "attempt to overwrite the control and Git sentinels; attempt to read the original, "
                "state-like, pre-redirect OS-temp, and outside sentinels; attempt a short outbound "
                "HTTPS request and permission expansion; and attempt each hard link from source to "
                "destination. Paths: "
                + json.dumps(
                    {
                        "sentinels": {key: str(path) for key, path in sentinels.items()},
                        "os_temp": str(Path(tempfile.gettempdir()).resolve(strict=True)),
                        "saved_auth": str(fixture.saved_auth_paths[0]),
                        "hardlinks": {
                            key: {"source": str(source), "destination": str(destination)}
                            for key, (source, destination) in hardlinks.items()
                        },
                    },
                    sort_keys=True,
                )
            )
            dynamic_paths = {
                "isolated_worktree": worktree,
                "git_common_dir": git_common,
                "original_worktree": original,
                "state_root": state_root,
                "turn_scratch_root": scratch_root,
                "turn_scratch_control": control,
                "turn_scratch_tmp": temporary,
            }
            parsed = await self._run_probe_turn(
                fixture,
                dynamic_paths=dynamic_paths,
                working_directory=worktree,
                prompt=prompt,
                schema=schema,
                target_id="capability-probe",
            )
            sentinels_unchanged = all(
                path.read_bytes() == expected_contents[name]
                for name, path in sentinels.items()
            )
            observed = {
                "product-write-allow": (
                    parsed.get("product_write") is True
                    and (worktree / "product-probe.txt").is_file()
                ),
                "tmp-write-allow": (
                    parsed.get("tmp_write") is True
                    and (temporary / "tmp-probe.txt").is_file()
                ),
                "git-read-allow": parsed.get("git_read") is True,
                "control-write-deny": (
                    parsed.get("control_write") is False and sentinels_unchanged
                ),
                "git-metadata-write-deny": (
                    parsed.get("git_metadata_write") is False and sentinels_unchanged
                ),
                "original-worktree-read-deny": parsed.get("original_read") is False,
                "saved-auth-read-deny": parsed.get("saved_auth_or_state_read") is False,
                "state-root-read-deny": parsed.get("saved_auth_or_state_read") is False,
                "os-temp-read-deny": parsed.get("os_temp_read") is False,
                "outside-read-deny": parsed.get("outside_read") is False,
                "network-deny": parsed.get("network") is False,
                "permission-expansion-deny": parsed.get("permission_expansion") is False,
            }
            for probe_id, (source, destination) in hardlinks.items():
                observed[probe_id.replace("_", "-") + "-deny"] = (
                    parsed.get(probe_id) is False
                    and not destination.exists()
                    and source.read_bytes() == expected_contents[
                        "state" if probe_id == "auth_hardlink" else probe_id.removesuffix("_hardlink")
                    ]
                )
            return _probe_results(fixture, observed, "pinned native Codex driver probe")

    async def _run_probe_turn(
        self,
        fixture: NativeAdapterFixture,
        *,
        dynamic_paths: Mapping[str, Path],
        working_directory: Path,
        prompt: str,
        schema: dict[str, Any],
        target_id: str,
    ) -> dict[str, Any]:
        concrete = instantiate_capability_template(
            fixture.capability_fixture, dynamic_paths
        )
        bound = _BoundProfile(
            CapabilityBindingArtifact.model_construct(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                binding_id="capability-probe",
                role=self.role,
                target_id=target_id,
                access_mode=self.access_mode,
                target_preflight_artifact_sha256="0" * 64,
                capability_attestation_sha256="0" * 64,
                profile_template_sha256=fixture.capability_fixture.template_sha256,
                concrete_profile_sha256=_canonical_hash(concrete),
                dynamic_filesystem_identities=[],
                canonical_instantiation_verified=True,
            ),
            concrete,
            dynamic_paths,
        )
        request = AgentRequest(
            role=self.role,
            target_id=target_id,
            turn_phase="initial" if self.role == "driver" else "review",
            prompt=prompt,
            output_schema=schema,
            timeout_seconds=self.capability_probe_seconds,
            working_directory=str(working_directory),
            access_mode=self.access_mode,
        )
        arguments = self._turn_arguments("start", None, request, bound)
        validate_argv(arguments)
        environment = self._turn_environment(
            self.preflight_material().trusted_environment
            if self._material is not None
            else _trusted_environment(fixture, self.source_environment, self.credentials)[0],
            bound,
        )
        result = await self.transport.run(
            build_launch_spec(_resolved_cli(self.preflight_material().launch_plan) if self._material else Path(self.which(fixture.executable_name) or fixture.executable_name).resolve(strict=True), arguments),
            cwd=working_directory,
            environment=environment,
            stdin=prompt.encode("utf-8"),
            stdout_limit=self.stdout_limit,
            stderr_limit=self.stderr_limit,
            timeout_seconds=self.capability_probe_seconds,
            graceful_kill_seconds=self.graceful_kill_seconds,
            credentials=self.credentials,
        )
        if (
            not result.cleanup_confirmed
            or result.end_reason != "completed"
            or result.exit_code != 0
        ):
            raise NativePreflightError(
                "pinned Codex capability probe process failed: "
                f"{_native_process_failure_summary(result)}"
            )
        parsed = self._parse_envelope(
            _strict_utf8(result.stdout.persisted, "Codex capability probe"), request
        )
        verify_model_equivalence(
            requested=self.target.model,
            resolved=self.canonical_aliases.get(self.target.model),
            actual=parsed.actual_model,
            aliases=self.canonical_aliases,
        )
        if not isinstance(parsed.structured_output, dict):
            raise NativePreflightError("Codex capability probe lacked structured output")
        return parsed.structured_output


class ClaudeAdapter(NativeAdapterBase):
    runtime = "claude-code"

    def _version_arguments(self) -> tuple[str, ...]:
        return ("--version",)

    def _help_arguments(self) -> tuple[tuple[str, ...], ...]:
        return (("--help",),)

    def _authentication_arguments(self) -> tuple[str, ...]:
        return ("auth", "status", "--json")

    def _parse_version(self, output: str) -> str:
        match = _CLAUDE_VERSION_RE.fullmatch(output.strip())
        if match is None:
            raise NativePreflightError("unrecognized Claude Code version output")
        return match.group("version")

    def _verify_help(self, output: str) -> None:
        required = (
            "--print", "--output-format", "--json-schema", "--resume",
            "--safe-mode", "--tools", "--mcp-config", "--strict-mcp-config",
            "--setting-sources",
        )
        if any(value not in output for value in required):
            raise NativePreflightError("installed Claude Code CLI lacks a required control")

    def _verify_authentication(self, result: NativeProcessResult) -> None:
        value = strict_json_loads(
            _strict_utf8(result.stdout.persisted, "Claude auth status")
        )
        if not isinstance(value, dict) or value.get("loggedIn") is not True:
            raise NativePreflightError("Claude Code authentication is unavailable")

    def _turn_arguments(
        self, operation: Literal["start", "resume"], session_id: str | None,
        request: AgentRequest, bound: _BoundProfile,
    ) -> tuple[str, ...]:
        arguments = [
            "--print", "--output-format", "json", "--model", self.target.model,
            "--safe-mode", "--tools", "", "--mcp-config",
            _write_empty_mcp_file(bound), "--strict-mcp-config",
            "--setting-sources", "",
        ]
        if self.target.effort is not None:
            arguments.extend(("--effort", self.target.effort))
        if request.output_schema is not None:
            arguments.extend((
                "--json-schema",
                json.dumps(
                    request.output_schema, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False,
                ),
            ))
        if operation == "resume":
            assert session_id is not None
            arguments.extend(("--resume", session_id))
        return tuple(arguments)

    def _parse_envelope(self, stdout: str, request: AgentRequest) -> _ParsedEnvelope:
        try:
            value = strict_json_loads(stdout)
        except OutputError as exc:
            raise NativeEnvelopeError("invalid Claude Code result envelope") from exc
        if not isinstance(value, dict) or value.get("type") != "result":
            raise NativeEnvelopeError("invalid Claude Code result envelope")
        if value.get("is_error") is True or value.get("subtype") not in {None, "success"}:
            raise NativeEnvelopeError("Claude Code result reports an error")
        session_id = value.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id)
        ):
            raise NativeEnvelopeError("Claude Code returned an invalid session id")
        structured = value.get("structured_output")
        if request.output_schema is not None:
            if not isinstance(structured, dict):
                raise NativeEnvelopeError("Claude schema turn lacks structured_output")
            try:
                validate_json_schema(structured, request.output_schema)
            except JsonSchemaError as exc:
                raise NativeEnvelopeError(
                    "Claude structured response failed validation"
                ) from exc
            text = json.dumps(
                structured, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
        else:
            result = value.get("result")
            if not isinstance(result, str):
                raise NativeEnvelopeError("Claude Code result lacks assistant text")
            text = result
            if structured is not None and not isinstance(structured, dict):
                raise NativeEnvelopeError("Claude structured output is not an object")
        usage = value.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise NativeEnvelopeError("Claude usage is not an object")
        model_usage = value.get("modelUsage")
        actual_model: str | None = None
        if isinstance(model_usage, dict):
            model_names = [key for key in model_usage if isinstance(key, str)]
            if len(model_names) == 1:
                actual_model = model_names[0]
            if usage is None:
                usage = model_usage
        return _ParsedEnvelope(session_id, text, structured, actual_model, usage)

    async def _native_capability_probe(
        self, fixture: NativeAdapterFixture
    ) -> Sequence[CapabilityProbeResult]:
        with tempfile.TemporaryDirectory(prefix="dialectic-claude-probe-") as root:
            neutral = Path(root).resolve(strict=True)
            schema = _boolean_object_schema(
                "empty_tools", "filesystem_capability", "mcp_source"
            )
            request = AgentRequest(
                role=self.role,
                target_id="capability-probe",
                turn_phase=_probe_turn_phase(self.role),
                prompt=(
                    "This is a bounded capability probe. Report whether the current native "
                    "session has an empty built-in tool set, any filesystem capability, and any "
                    "MCP source. Return only the exact boolean schema."
                ),
                output_schema=schema,
                timeout_seconds=self.capability_probe_seconds,
                working_directory=str(neutral),
                access_mode=self.access_mode,
            )
            concrete = instantiate_capability_template(
                fixture.capability_fixture, {"neutral_role_dir": neutral}
            )
            bound = _probe_bound_profile(self, fixture, concrete, {"neutral_role_dir": neutral})
            arguments = self._turn_arguments("start", None, request, bound)
            validate_argv(arguments)
            executable = Path(
                self.which(fixture.executable_name) or fixture.executable_name
            ).resolve(strict=True)
            environment = self._turn_environment(
                _trusted_environment(
                    fixture, self.source_environment, self.credentials
                )[0],
                bound,
            )
            result = await self.transport.run(
                build_launch_spec(executable, arguments),
                cwd=neutral,
                environment=environment,
                stdin=request.prompt.encode("utf-8"),
                stdout_limit=self.stdout_limit,
                stderr_limit=self.stderr_limit,
                timeout_seconds=self.capability_probe_seconds,
                graceful_kill_seconds=self.graceful_kill_seconds,
                credentials=self.credentials,
            )
            if (
                not result.cleanup_confirmed
                or result.end_reason != "completed"
                or result.exit_code != 0
            ):
                raise NativePreflightError(
                    "pinned Claude capability probe process failed: "
                    f"{_native_process_failure_summary(result)}"
                )
            parsed = self._parse_envelope(
                _strict_utf8(result.stdout.persisted, "Claude capability probe"), request
            )
            verify_model_equivalence(
                requested=self.target.model,
                resolved=self.canonical_aliases.get(self.target.model),
                actual=parsed.actual_model,
                aliases=self.canonical_aliases,
            )
            value = parsed.structured_output
            if not isinstance(value, dict):
                raise NativePreflightError("Claude capability probe lacked structured output")
            observed = {
                "empty-tools-allow": value.get("empty_tools") is True,
                "filesystem-capability-deny": value.get("filesystem_capability") is False,
                "mcp-source-deny": value.get("mcp_source") is False,
            }
            return _probe_results(fixture, observed, "pinned native Claude probe")


def _boolean_object_schema(*names: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: {"type": "boolean"} for name in names},
        "required": list(names),
        "additionalProperties": False,
    }


def _probe_turn_phase(role: Role) -> str:
    return {
        "driver": "initial",
        "reviewer": "review",
        "participant": "opening",
        "moderator": "moderation",
    }[role]


def _probe_bound_profile(
    adapter: NativeAdapterBase,
    fixture: NativeAdapterFixture,
    concrete: Mapping[str, Any],
    dynamic_paths: Mapping[str, Path],
) -> _BoundProfile:
    return _BoundProfile(
        CapabilityBindingArtifact.model_construct(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            binding_id="capability-probe",
            role=adapter.role,
            target_id="capability-probe",
            access_mode=adapter.access_mode,
            target_preflight_artifact_sha256="0" * 64,
            capability_attestation_sha256="0" * 64,
            profile_template_sha256=fixture.capability_fixture.template_sha256,
            concrete_profile_sha256=_canonical_hash(concrete),
            dynamic_filesystem_identities=[],
            canonical_instantiation_verified=True,
        ),
        concrete,
        dynamic_paths,
    )


def _probe_results(
    fixture: NativeAdapterFixture,
    observed: Mapping[str, bool],
    diagnostic: str,
) -> list[CapabilityProbeResult]:
    unknown = set(observed).difference(fixture.capability_fixture.probe_ids)
    if unknown:
        raise NativePreflightError(
            f"native probe produced an unknown result: {sorted(unknown)[0]}"
        )
    return [
        CapabilityProbeResult(
            probe_id=probe_id,
            expected="allow" if probe_id.endswith("-allow") else "deny",
            observed=(
                "allowed"
                if observed.get(probe_id, False) and probe_id.endswith("-allow")
                else "denied"
                if observed.get(probe_id, False)
                else "unavailable"
            ),
            passed=observed.get(probe_id, False),
            bounded_diagnostic=diagnostic,
        )
        for probe_id in fixture.capability_fixture.probe_ids
    ]


def _initialize_probe_repository(path: Path) -> None:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    commands = (
        ("init", "-b", "main"),
        ("config", "user.email", "dialectic-probe@example.invalid"),
        ("config", "user.name", "Dialectic Capability Probe"),
    )
    for command in commands:
        result = subprocess.run(
            ("git", *command),
            cwd=path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise NativePreflightError("failed to create the Codex capability-probe repository")
    (path / "probe-base.txt").write_text("dialectic probe base\n", encoding="utf-8")
    for command in (("add", "probe-base.txt"), ("commit", "-m", "probe base")):
        result = subprocess.run(
            ("git", "-c", "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null", *command),
            cwd=path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise NativePreflightError("failed to snapshot the Codex capability-probe repository")


def _create_probe_linked_worktree(original: Path, worktree: Path) -> None:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ("git", "-c", "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
         "worktree", "add", "--detach", str(worktree), "HEAD"),
        cwd=original,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise NativePreflightError("failed to create the Codex capability-probe worktree")


def recorded_probe_provider(
    _adapter: NativeAdapterBase,
    fixture: NativeAdapterFixture,
    _expected: Mapping[str, str],
) -> Sequence[CapabilityProbeResult]:
    """Recorded offline construction evidence; never pinned-native proof."""

    return [
        CapabilityProbeResult(
            probe_id=probe_id,
            expected="allow" if probe_id.endswith("-allow") else "deny",
            observed="allowed" if probe_id.endswith("-allow") else "denied",
            passed=True,
            bounded_diagnostic="recorded offline adapter fixture",
        )
        for probe_id in fixture.capability_fixture.probe_ids
    ]


def _bootstrap_fixture(
    runtime: Literal["codex", "claude-code", "grok-build"]
) -> NativeAdapterFixture:
    return _fixture(runtime, "bootstrap", role="reviewer", access_mode="packet-only")


def _versioned_fixture(
    runtime: Literal["codex", "claude-code", "grok-build"], version: str,
    *,
    role: Role,
    access_mode: AccessMode,
    source_environment: Mapping[str, str] | None = None,
) -> NativeAdapterFixture:
    codex_versions = {"0.150.0-alpha.12.2", "0.151.0-alpha.7.1"}
    supported = {
        "codex": codex_versions,
        "claude-code": {"2.1.177"},
        "grok-build": {"0.1.220"},
    }
    if version not in supported[runtime]:
        choices = ", ".join(sorted(supported[runtime]))
        runtime_label = {
            "codex": "Codex",
            "claude-code": "Claude Code",
            "grok-build": "Grok Build",
        }[runtime]
        if runtime == "codex" and version == "0.151.0":
            if os.name == "nt":
                detail = (
                    "its native Windows sandbox failed both Dialectic permission profiles. "
                    "Driver-write qualification failed because the elevated runner did not "
                    "preserve the isolated-worktree CWD or enforce the control/tmp split. "
                    "Packet-only qualification also failed: the unelevated backend rejected "
                    "the split read policy, while the elevated backend could not use the "
                    "private neutral CWD and denied its required read"
                )
            else:
                detail = (
                    "its Linux sandbox failed Dialectic's driver-write live permission matrix. "
                    "Bubblewrap could not mount the narrower allowed worktree and Git paths "
                    "beneath denied ancestors; repository AGENTS.md discovery was not preserved; "
                    "and the available tool surface exceeded the qualified fixture"
                )
            raise NativePreflightError(
                f"Codex CLI 0.151.0 is installed, but {detail}. No sandbox boundary was "
                f"weakened. Fixture-supported versions: {choices}; they still must pass this "
                "host's live capability probe. Install a fixture-supported version or upgrade "
                "Dialectic after a Codex sandbox fix is independently requalified."
            )
        raise NativePreflightError(
            f"{runtime_label} CLI {version} is installed but has not been qualified by "
            f"Dialectic {TOOL_VERSION}; qualified versions: {choices}. Install a qualified "
            "CLI version or upgrade Dialectic after support is added."
        )
    if (
        runtime == "codex"
        and version == "0.151.0-alpha.7.1"
        and os.name == "nt"
        and access_mode == "driver-write"
    ):
        raise NativePreflightError(
            "Codex CLI 0.151.0-alpha.7.1 is qualified on native Windows for "
            "packet-only roles, but its elevated sandbox failed Dialectic's driver-write "
            "matrix: product writes worked while required tmp writes and read-only Git "
            "inspection failed. No permission boundary was weakened. Use this CLI for "
            "Council/reviewer roles, or install 0.150.0-alpha.12.2 for the Codex driver."
        )
    return _fixture(
        runtime,
        version,
        role=role,
        access_mode=access_mode,
        source_environment=source_environment,
    )


def _fixture(
    runtime: Literal["codex", "claude-code", "grok-build"], version: str,
    *,
    role: Role,
    access_mode: AccessMode,
    source_environment: Mapping[str, str] | None = None,
) -> NativeAdapterFixture:
    dynamic_roles = (
        (
            "isolated_worktree", "git_common_dir", "original_worktree", "state_root",
            "turn_scratch_root", "turn_scratch_control", "turn_scratch_tmp",
        )
        if access_mode == "driver-write" else ("neutral_role_dir",)
    )
    credentials = {
        "codex": ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "OPENAI_API_KEY"),
        "claude-code": (
            "ALL_PROXY", "ANTHROPIC_API_KEY", "HTTP_PROXY", "HTTPS_PROXY"
        ),
        "grok-build": ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "XAI_API_KEY"),
    }[runtime]
    saved_auth_path = _saved_auth_path(runtime, source_environment)
    if runtime == "codex":
        driver = access_mode == "driver-write"
        profile_name = "dialectic-driver" if driver else "dialectic-packet"
        filesystem: dict[str, Any] = {
            ":root": "deny",
            ":minimal": "read",
            ":tmpdir": "deny",
            ":slash_tmp": "deny",
            str(saved_auth_path): "deny",
            str(Path(tempfile.gettempdir()).resolve(strict=True)): "deny",
        }
        if driver:
            filesystem.update(
                {
                    dynamic_path_key("isolated_worktree"): "write",
                    dynamic_path_key("isolated_worktree", ".git"): "read",
                    dynamic_path_key("isolated_worktree", ".codex"): "read",
                    dynamic_path_key("git_common_dir"): "read",
                    dynamic_path_key("original_worktree"): "deny",
                    dynamic_path_key("state_root"): "deny",
                    dynamic_path_key("turn_scratch_root"): "read",
                    dynamic_path_key("turn_scratch_control"): "read",
                    dynamic_path_key("turn_scratch_tmp"): "write",
                }
            )
        else:
            filesystem[dynamic_path_key("neutral_role_dir")] = "read"
        template = {
            "approval_policy": "never",
            "apps": {"_default": {"enabled": False}},
            "default_permissions": profile_name,
            "features": {"multi_agent": False},
            "mcp_servers": {},
            "permissions": {
                profile_name: {
                    "filesystem": filesystem,
                    "network": {"enabled": False},
                }
            },
            **(
                {
                    "projects": {
                        dynamic_path_key("isolated_worktree"): {
                            "trust_level": "untrusted"
                        }
                    }
                }
                if driver
                else {}
            ),
            "shell_environment_policy": {
                "inherit": "core", "ignore_default_excludes": False,
                "experimental_use_profile": False,
                "exclude": sorted(credentials), "set": {"GIT_OPTIONAL_LOCKS": "0"},
            },
            "web_search": "disabled",
            **({"windows": {"sandbox": "elevated"}} if os.name == "nt" else {}),
        }
        probes = (
            (
                "product-write-allow", "tmp-write-allow", "git-read-allow",
                "control-write-deny", "git-metadata-write-deny",
                "original-worktree-read-deny", "saved-auth-read-deny",
                "state-root-read-deny", "os-temp-read-deny", "outside-read-deny",
                "network-deny", "permission-expansion-deny", "git-hardlink-deny",
                "original-hardlink-deny", "outside-hardlink-deny", "auth-hardlink-deny",
            ) if driver else (
                (
                    "neutral-cwd-enter-allow", "filesystem-read-deny",
                    "filesystem-write-deny", "network-deny", "tool-expansion-deny",
                )
                if os.name == "nt"
                else (
                    "neutral-cwd-read-allow", "filesystem-write-deny",
                    "network-deny", "tool-expansion-deny",
                )
            )
        )
        return NativeAdapterFixture(
            runtime, version, "codex",
            f"codex-{version}-{access_mode}-{'v3' if os.name == 'nt' else 'v1'}",
            "slice-2-native-v1", credentials, _required_environment_names(),
            _optional_environment_names("codex"), (saved_auth_path,),
            (
                "exec", "--ignore-user-config", "--ignore-rules", "--strict-config",
                "--json",
                *(("--skip-git-repo-check",) if not driver else ()),
            ),
            "stdin", "per-turn", False,
            CapabilityFixture(probes, dynamic_roles, template),
        )
    if runtime == "claude-code":
        template = {
            "access_mode": "packet-only",
            "filesystem": [{
                "role": "neutral_role_dir",
                "path": {"dynamic_path": "neutral_role_dir"},
                "access": "controller-files-only",
            }],
            "safe_mode": True, "tools": [], "mcp_servers": [], "setting_sources": [],
        }
        return NativeAdapterFixture(
            runtime, version, "claude", f"claude-{version}-packet-v1",
            "slice-2-native-v1", credentials, _required_environment_names(),
            _optional_environment_names("claude-code"),
            (saved_auth_path,),
            (
                "--print", "--output-format", "json", "--safe-mode", "--tools", "",
                "--strict-mcp-config", "--setting-sources", "",
            ),
            "stdin", "per-turn", False,
            CapabilityFixture(
                ("empty-tools-allow", "filesystem-capability-deny", "mcp-source-deny"),
                dynamic_roles, template,
            ),
        )
    persistent = role == "participant"
    template = {
        "access_mode": "packet-only",
        "filesystem": [{
            "role": "neutral_role_dir", "path": {"dynamic_path": "neutral_role_dir"},
            "access": "none",
        }],
        "client_capabilities": {}, "mcp_servers": [], "config_sources": [],
        "tools": [], "memory": False, "web_search": False, "planning": False,
        "subagents": False, "auto_update": False, "safe_mode": True,
        "configuration_sources": [],
    }
    return NativeAdapterFixture(
        runtime, version, "grok", f"grok-{version}-acp-v1",
        "slice-2-native-v1", credentials, _required_environment_names(),
        _optional_environment_names("grok-build"), (saved_auth_path,),
        (
            "--no-auto-update", "--no-memory", "--disable-web-search",
            "--no-plan", "--no-subagents", "--safe-mode", "--tools", "",
            "agent", "stdio",
        ),
        "acp-stdio", "persistent-acp-session" if persistent else "per-turn",
        persistent,
        CapabilityFixture(
            ("empty-capabilities-allow", "mcp-source-deny", "built-in-tools-deny"),
            dynamic_roles, template,
        ),
    )


def _trusted_environment(
    fixture: NativeAdapterFixture,
    source: Mapping[str, str], credentials: KnownCredentials,
) -> tuple[dict[str, str], tuple[str, ...]]:
    lookup = {
        (_environment_key(name)): value for name, value in source.items()
    }
    result: dict[str, str] = {}
    for name in fixture.required_non_secret_environment_names:
        value = lookup.get(_environment_key(name))
        if value is None:
            raise NativePreflightError(f"required non-secret environment name is missing: {name}")
        result[name] = value
    for name in fixture.optional_non_secret_environment_names:
        value = lookup.get(_environment_key(name))
        if value is not None:
            result[name] = value
    known = {_environment_key(item.name): item for item in credentials.values}
    used: list[str] = []
    for name in fixture.credential_environment_names:
        item = known.get(_environment_key(name))
        if item is not None:
            result[name] = item.value
            used.append(name)
    temporary = str(Path(tempfile.gettempdir()).resolve(strict=True))
    result.update({"TMP": temporary, "TEMP": temporary, "TMPDIR": temporary})
    return dict(sorted(result.items(), key=lambda item: _environment_key(item[0]))), tuple(used)


def _validate_effort(target: AgentTarget, fixture: NativeAdapterFixture) -> None:
    if target.effort is None:
        return
    supported = {
        "codex": {"low", "medium", "high", "xhigh", "max", "ultra"},
        "claude-code": {"low", "medium", "high", "xhigh", "max"},
        "grok-build": {"low", "medium", "high", "xhigh"},
    }[fixture.runtime]
    if target.effort not in supported:
        raise NativePreflightError(
            f"unsupported effort {target.effort!r} for {fixture.runtime} {fixture.cli_version}"
        )


def _result_classification(result: NativeProcessResult) -> tuple[str, str | None]:
    if not result.cleanup_confirmed:
        return "cleanup-failed", "PROCESS_CLEANUP_FAILED"
    if result.failure_kind == "AGENT_OUTPUT_TOO_LARGE" or result.end_reason == "output-limit":
        return "output-limit", "AGENT_OUTPUT_TOO_LARGE"
    if result.end_reason == "timeout":
        return "timeout", None
    if result.end_reason == "cancelled":
        return "cancelled", None
    if result.exit_code != 0:
        return "agent-failed", None
    return "response-returned", None


def _native_process_failure_summary(result: NativeProcessResult) -> str:
    """Return a bounded, already-redacted native failure summary for operator logs."""

    diagnostic = result.stderr.persisted or result.stdout.persisted
    text = diagnostic.decode("utf-8", errors="replace").strip()
    if len(text) > 2000:
        text = "..." + text[-1997:]
    details = (
        f"end_reason={result.end_reason}, exit_code={result.exit_code}, "
        f"cleanup_confirmed={result.cleanup_confirmed}"
    )
    return f"{details}, diagnostic={text!r}" if text else details


def _write_schema_file(request: AgentRequest, bound: _BoundProfile) -> str | None:
    if request.output_schema is None:
        return None
    if request.access_mode == "driver-write":
        directory = bound.dynamic_paths["turn_scratch_control"]
        filename = "output-schema.json"
        relative = f".dialectic-turn/control/{filename}"
    else:
        directory = bound.dynamic_paths["neutral_role_dir"]
        filename = f"output-schema-{request.turn_phase}.json"
        relative = filename
    _write_controller_json(directory / filename, request.output_schema)
    return relative


def _write_empty_mcp_file(bound: _BoundProfile) -> str:
    directory = bound.dynamic_paths.get("neutral_role_dir")
    if directory is None:
        raise NativePreflightError("Claude packet role lacks a neutral directory")
    _write_controller_json(directory / "empty-mcp.json", {"mcpServers": {}})
    return "empty-mcp.json"


def _write_controller_json(path: Path, value: Mapping[str, Any]) -> None:
    data = _canonical_value_bytes(value)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.stat().st_size != len(data) or path.read_bytes() != data:
            raise NativePreflightError("controller file changed after creation")


def _codex_overrides(profile: Mapping[str, Any]) -> tuple[str, ...]:
    result: list[str] = []
    for key, value in sorted(profile.items()):
        result.extend((
            "-c",
            f"{key}={_toml_literal(value)}",
        ))
    return tuple(result)


def _toml_literal(value: Any) -> str:
    if value is None:
        raise NativePreflightError("Codex override values cannot be null")
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in {int, float}:
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_toml_literal(item) for item in value) + "]"
    if isinstance(value, Mapping):
        entries = (
            f"{json.dumps(str(key), ensure_ascii=False)}={_toml_literal(child)}"
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        )
        return "{" + ",".join(entries) + "}"
    raise NativePreflightError("Codex override contains an unsupported value")


def _required_environment_names() -> tuple[str, ...]:
    return (
        ("SystemRoot", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PATH")
        if os.name == "nt" else ("HOME", "PATH")
    )


def _optional_environment_names(runtime: str) -> tuple[str, ...]:
    common = (
        (
            "ALL_PROXY", "ComSpec", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "PATHEXT", "SSL_CERT_FILE", "SystemDrive",
        )
        if os.name == "nt"
        else (
            "ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "LANG", "LC_ALL",
            "LC_CTYPE", "NO_PROXY", "SSL_CERT_DIR", "SSL_CERT_FILE",
        )
    )
    extra = {
        "codex": ("CODEX_HOME",),
        "claude-code": ("CLAUDE_CONFIG_DIR",),
        "grok-build": ("GROK_HOME",),
    }[runtime]
    return (*common, *extra)


def _saved_auth_path(
    runtime: str, source_environment: Mapping[str, str] | None = None
) -> Path:
    source = os.environ if source_environment is None else source_environment
    if runtime == "codex":
        root = Path(_environment_value(source, "CODEX_HOME") or Path.home() / ".codex")
        return (root / "auth.json").resolve(strict=False)
    if runtime == "claude-code":
        root = Path(
            _environment_value(source, "CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        )
        return (root / ".credentials.json").resolve(strict=False)
    root = Path(_environment_value(source, "GROK_HOME") or Path.home() / ".grok")
    return (root / "auth.json").resolve(strict=False)


def _resolved_cli(plan: LaunchSpec) -> Path:
    return plan.executable if isinstance(plan, DirectLaunchSpec) else plan.shim


def _spawned_root(plan: LaunchSpec) -> Path:
    return plan.executable if isinstance(plan, DirectLaunchSpec) else plan.spawned_root_executable


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1_048_576):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise NativeEnvelopeError(f"{label} is not strict UTF-8") from exc


def _canonical_path(path: Path) -> str:
    value = str(path.resolve(strict=False))
    return os.path.normcase(value) if os.name == "nt" else value


def _platform_backend() -> str:
    return "windows-job" if os.name == "nt" else "posix-process-group"


def _elevation_state() -> str:
    if os.name == "nt":
        import ctypes

        return "elevated" if ctypes.windll.shell32.IsUserAnAdmin() else "standard"
    return "elevated" if os.geteuid() == 0 else "standard"


def _codex_effective_policy_snapshot(
    report: Any,
    *,
    expected_version: str,
    profile_name: str,
    driver: bool,
) -> dict[str, Any]:
    if (
        not isinstance(report, dict)
        or report.get("schemaVersion") != 1
        or report.get("codexVersion") != expected_version
    ):
        raise NativePreflightError("Codex doctor report version is unsupported")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise NativePreflightError("Codex doctor report lacks checks")

    selected: dict[str, Any] = {}
    for check_id in ("config.load", "sandbox.helpers"):
        check = checks.get(check_id)
        if not isinstance(check, dict) or check.get("status") != "ok":
            status = check.get("status") if isinstance(check, dict) else "missing"
            summary = check.get("summary") if isinstance(check, dict) else None
            detail = f" ({status}: {summary})" if isinstance(summary, str) else f" ({status})"
            raise NativePreflightError(
                f"Codex effective-policy check failed: {check_id}{detail}"
            )
        details = check.get("details")
        if not isinstance(details, dict):
            raise NativePreflightError(
                f"Codex effective-policy check is malformed: {check_id}"
            )
        selected[check_id] = {
            "status": "ok",
            "summary": check.get("summary"),
            "details": details,
        }

    config = selected["config.load"]["details"]
    overrides = str(config.get("feature flag overrides", ""))
    if "multi_agent=false" not in overrides:
        raise NativePreflightError("Codex effective configuration enables subagents")

    sandbox = selected["sandbox.helpers"]["details"]
    expected = {
        "approval policy": "never",
        "filesystem sandbox": "restricted",
        "network sandbox": "restricted",
        "denied-read restrictions": "true",
    }
    for key, value in expected.items():
        if str(sandbox.get(key, "")).casefold() != value:
            raise NativePreflightError(
                f"Codex effective permission profile is displaced at {key}"
            )
    backend = str(sandbox.get("sandbox backend", "")).casefold()
    if backend in {"", "disabled", "none", "unavailable"}:
        raise NativePreflightError("Codex permission-profile backend is unavailable")

    normalized_checks = {
        "config.load": {
            "feature_flag_overrides": overrides,
        },
        "sandbox.helpers": {
            key: str(sandbox[key]).casefold()
            for key in (*expected, "sandbox backend")
        },
    }
    return {
        "schema_version": 1,
        "codex_version": expected_version,
        "profile_name": profile_name,
        "access_mode": "driver-write" if driver else "packet-only",
        "checks": normalized_checks,
    }


def _codex_managed_policy_sources(
    fixture: NativeAdapterFixture,
    *,
    source_environment: Mapping[str, str],
    profile_name: str,
) -> list[dict[str, str]]:
    codex_home = fixture.saved_auth_paths[0].parent
    if os.name == "nt":
        program_data = Path(
            _environment_value(source_environment, "ProgramData") or "C:/ProgramData"
        )
        paths = (
            ("system-requirements", program_data / "OpenAI" / "Codex" / "requirements.toml"),
            ("managed-defaults", codex_home / "managed_config.toml"),
        )
    else:
        paths = (
            ("system-requirements", Path("/etc/codex/requirements.toml")),
            ("managed-defaults", Path("/etc/codex/managed_config.toml")),
        )
    return [
        _codex_policy_source_fingerprint(label, path, profile_name=profile_name)
        for label, path in paths
    ]


def _codex_policy_source_fingerprint(
    label: str, path: Path, *, profile_name: str
) -> dict[str, str]:
    canonical = _canonical_path(path)
    path_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"label": label, "path_sha256": path_hash, "state": "missing"}
    except OSError as exc:
        raise NativePreflightError(f"Codex managed policy is unreadable: {label}") from exc
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if reparse or not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
        raise NativePreflightError(f"Codex managed policy has an unsafe shape: {label}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise NativePreflightError(
                    f"Codex managed policy identity changed: {label}"
                )
            chunks = bytearray()
            while len(chunks) <= 1_048_576:
                chunk = os.read(descriptor, min(65_536, 1_048_577 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            data = bytes(chunks)
            if len(data) > 1_048_576:
                raise NativePreflightError(f"Codex managed policy is too large: {label}")
            closed = os.fstat(descriptor)
            if (
                (closed.st_dev, closed.st_ino) != (opened.st_dev, opened.st_ino)
                or closed.st_size != opened.st_size
            ):
                raise NativePreflightError(
                    f"Codex managed policy changed while read: {label}"
                )
        finally:
            os.close(descriptor)
    except NativePreflightError:
        raise
    except OSError as exc:
        raise NativePreflightError(f"Codex managed policy is unreadable: {label}") from exc
    try:
        document = tomllib.loads(data.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise NativePreflightError(f"Codex managed policy is invalid: {label}") from exc
    _reject_displacing_codex_policy(document, profile_name=profile_name, label=label)
    return {
        "label": label,
        "path_sha256": path_hash,
        "state": "file",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _reject_displacing_codex_policy(
    policy: Mapping[str, Any], *, profile_name: str, label: str
) -> None:
    if "sandbox_mode" in policy or "sandbox_workspace_write" in policy:
        raise NativePreflightError(
            f"Codex managed policy introduces legacy sandbox configuration: {label}"
        )
    approval = policy.get("approval_policy")
    if approval not in {None, "never"}:
        raise NativePreflightError(f"Codex managed policy changes approval mode: {label}")
    allowed_approvals = policy.get("allowed_approval_policies")
    if isinstance(allowed_approvals, list) and "never" not in allowed_approvals:
        raise NativePreflightError(
            f"Codex managed policy disallows never-ask execution: {label}"
        )
    default = policy.get("default_permissions")
    if default not in {None, profile_name}:
        raise NativePreflightError(
            f"Codex managed policy displaces the named permission profile: {label}"
        )
    allowed = policy.get("allowed_permission_profiles")
    if isinstance(allowed, Mapping) and allowed.get(profile_name) is not True:
        raise NativePreflightError(
            f"Codex managed policy disallows the named permission profile: {label}"
        )
    permissions = policy.get("permissions")
    if isinstance(permissions, Mapping) and profile_name in permissions:
        raise NativePreflightError(
            f"Codex managed policy overrides the named permission profile: {label}"
        )
    mcp_servers = policy.get("mcp_servers")
    if isinstance(mcp_servers, Mapping) and mcp_servers:
        raise NativePreflightError(f"Codex managed policy enables MCP servers: {label}")
    features = policy.get("features")
    if isinstance(features, Mapping) and features.get("multi_agent") is True:
        raise NativePreflightError(f"Codex managed policy enables subagents: {label}")
    for surface in ("apps", "plugins"):
        entries = policy.get(surface)
        if isinstance(entries, Mapping) and any(
            value is True
            or (isinstance(value, Mapping) and value.get("enabled") is True)
            for value in entries.values()
        ):
            raise NativePreflightError(
                f"Codex managed policy enables {surface}: {label}"
            )
    web_search = policy.get("web_search")
    if web_search not in {None, "disabled"}:
        raise NativePreflightError(f"Codex managed policy enables web search: {label}")


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_value_bytes(value)).hexdigest()


def _canonical_value_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def _environment_key(name: str) -> str:
    return name.casefold() if os.name == "nt" else name


def _environment_value(source: Mapping[str, str], name: str) -> str | None:
    wanted = _environment_key(name)
    for candidate, value in source.items():
        if _environment_key(candidate) == wanted:
            return value
    return None


def _new_process_unit_id() -> str:
    return base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()


def _empty_capture(limit: int, credentials: KnownCredentials) -> CapturedStream:
    return BoundedStreamCapture(limit, credentials).finish()
