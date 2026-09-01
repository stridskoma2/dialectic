"""Workflow-neutral Gate A, Gate B, and turn-evidence support."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Hashable, Mapping, Sequence, TypeVar

from .adapters import AgentAdapter, AgentProcessError, ModelMismatchError
from .capabilities import (
    CapabilityEvidenceError,
    CapabilityFixture,
    instantiate_capability_template,
    validate_binding_identities,
)
from .config import validate_model_bounds
from .contracts import ARTIFACT_SCHEMA_VERSION, TOOL_VERSION, FailureKind
from .native_adapters import (
    NativeEnvelopeError,
    NativeInvocationEvidence,
    NativePreflightError,
    NativeTurnError,
)
from .research import persist_source_citations
from .schemas import (
    AgentRequest,
    AgentRequestArtifact,
    AgentResponse,
    AgentTarget,
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    CapabilityProbeResult,
    PreflightResult,
    StreamCaptureResult,
    TargetPreflightArtifact,
    TurnAttemptArtifact,
)
from .service import DialecticFailure, ExecutionContext
from .store import canonical_json_bytes

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_PreflightRequest = TypeVar("_PreflightRequest")


class TurnFailure(RuntimeError):
    def __init__(self, kind: FailureKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


def stage_preflight_requests(
    requests: Sequence[_PreflightRequest],
    *,
    cohort_key: Callable[[_PreflightRequest], Hashable],
) -> tuple[tuple[_PreflightRequest, ...], tuple[_PreflightRequest, ...]]:
    """Run one cache-populating request per capability cohort before its peers."""

    leaders: list[_PreflightRequest] = []
    followers: list[_PreflightRequest] = []
    seen: set[Hashable] = set()
    for request in requests:
        key = cohort_key(request)
        if key in seen:
            followers.append(request)
        else:
            seen.add(key)
            leaders.append(request)
    return tuple(leaders), tuple(followers)


@dataclass(frozen=True, slots=True)
class GateAEvidence:
    preflight: TargetPreflightArtifact
    preflight_bytes: bytes
    preflight_sha256: str
    preflight_relative_path: str
    attestation: CapabilityAttestationArtifact
    attestation_bytes: bytes
    fixture: CapabilityFixture


@dataclass(frozen=True, slots=True)
class TurnResult:
    response: AgentResponse
    attempt: TurnAttemptArtifact


class WorkflowEvidenceSupport:
    """Persist and verify evidence shared by bounded workflows."""

    def persist_gate_a(
        self,
        context: ExecutionContext,
        *,
        adapter: AgentAdapter,
        role: str,
        target_id: str,
        target: AgentTarget,
        result: PreflightResult,
        access_mode: str,
    ) -> GateAEvidence:
        material_reader = getattr(adapter, "preflight_material", None)
        material = material_reader() if callable(material_reader) else None
        if material is not None:
            if material.process_lifecycle == "persistent-acp-session" and not (
                role == "participant"
                and material.prompt_transport == "acp-stdio"
                and material.process_local_continuation
            ):
                raise DialecticFailure(
                    "PREFLIGHT_FAILED",
                    "persistent native lifecycle lacks its fixture-qualified ACP property",
                )
            attestation = material.attestation
            attestation_bytes = canonical_json_bytes(attestation)
            attestation_sha = hashlib.sha256(attestation_bytes).hexdigest()
            context.service.store.write_capability_attestation(
                attestation_sha, attestation
            )
            launch_kind = (
                "windows-batch-shim"
                if type(material.launch_plan).__name__ == "WindowsBatchLaunchSpec"
                else "direct"
            )
            preflight = TargetPreflightArtifact(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                role=role,
                target_id=target_id,
                target=target,
                resolved_executable=str(material.resolved_executable),
                resolved_executable_identity=material.resolved_executable_identity,
                resolved_executable_sha256=material.resolved_executable_sha256,
                spawned_root_executable=str(material.spawned_root_executable),
                spawned_root_identity=material.spawned_root_identity,
                spawned_root_sha256=material.spawned_root_sha256,
                launch_kind=launch_kind,
                cli_version=material.cli_version,
                prompt_transport=material.prompt_transport,
                process_lifecycle=material.process_lifecycle,
                effective_static_flags=list(material.effective_static_flags),
                credential_env_names=list(material.credential_environment_names),
                denied_credential_path_sha256s=list(
                    material.denied_credential_path_sha256s
                ),
                adapter_fixture_version=material.adapter_fixture_version,
                capability_attestation_sha256=attestation_sha,
                authentication_verified=True,
            )
            relative = f"audit/targets/{role}/{target_id}.json"
            preflight_sha = context.service.store.write_artifact(
                context.handle, relative, preflight
            )
            preflight_bytes = context.service.store.read_artifact(
                context.handle, relative, 1_048_576
            )
            return GateAEvidence(
                preflight=preflight,
                preflight_bytes=preflight_bytes,
                preflight_sha256=preflight_sha,
                preflight_relative_path=relative,
                attestation=attestation,
                attestation_bytes=attestation_bytes,
                fixture=material.fixture,
            )

        dynamic_roles = (
            (
                "isolated_worktree",
                "git_common_dir",
                "original_worktree",
                "state_root",
                "turn_scratch_root",
                "turn_scratch_control",
                "turn_scratch_tmp",
            )
            if access_mode == "driver-write"
            else ("neutral_role_dir",)
        )
        fixture = CapabilityFixture(
            probe_ids=("offline-construction",),
            dynamic_roles=dynamic_roles,
            template={
                "access_mode": access_mode,
                "filesystem": [
                    {"role": name, "path": {"dynamic_path": name}}
                    for name in dynamic_roles
                ],
            },
        )
        probe_results = [
            CapabilityProbeResult(
                probe_id="offline-construction",
                expected="allow",
                observed="allowed",
                passed=True,
                bounded_diagnostic=None,
            )
        ]
        executable_key = f"scripted:{target.runtime}:v1"
        executable_sha = hashlib.sha256(executable_key.encode("utf-8")).hexdigest()
        results_sha = _canonical_hash(
            [result.model_dump(mode="json") for result in probe_results]
        )
        attestation = CapabilityAttestationArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            runtime=target.runtime,
            executable_identity=executable_key,
            executable_sha256=executable_sha,
            spawned_root_identity=executable_key,
            spawned_root_sha256=executable_sha,
            cli_version="scripted-offline-v1",
            platform_backend=_platform_backend(),
            elevation_state="offline-construction",
            adapter_fixture_version="scripted-offline-v1",
            fixture_test_version="slice-1-v1",
            profile_template_sha256=fixture.template_sha256,
            managed_policy_sha256=hashlib.sha256(b"offline-managed-policy\n").hexdigest(),
            probe_results=probe_results,
            probe_results_sha256=results_sha,
        )
        attestation_bytes = canonical_json_bytes(attestation)
        attestation_sha = hashlib.sha256(attestation_bytes).hexdigest()
        context.service.store.write_capability_attestation(attestation_sha, attestation)
        persistent_session = role == "participant" and adapter.process_local_continuation
        preflight = TargetPreflightArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            role=role,
            target_id=target_id,
            target=target,
            resolved_executable=executable_key,
            resolved_executable_identity=executable_key,
            resolved_executable_sha256=executable_sha,
            spawned_root_executable=executable_key,
            spawned_root_identity=executable_key,
            spawned_root_sha256=executable_sha,
            launch_kind="direct",
            cli_version="scripted-offline-v1",
            prompt_transport=adapter.prompt_transport,
            process_lifecycle=(
                "persistent-acp-session" if persistent_session else "per-turn"
            ),
            effective_static_flags=["offline-construction"],
            credential_env_names=[],
            denied_credential_path_sha256s=[],
            adapter_fixture_version="scripted-offline-v1",
            capability_attestation_sha256=attestation_sha,
            authentication_verified=True,
        )
        relative = f"audit/targets/{role}/{target_id}.json"
        preflight_sha = context.service.store.write_artifact(
            context.handle, relative, preflight
        )
        preflight_bytes = context.service.store.read_artifact(
            context.handle, relative, 1_048_576
        )
        return GateAEvidence(
            preflight=preflight,
            preflight_bytes=preflight_bytes,
            preflight_sha256=preflight_sha,
            preflight_relative_path=relative,
            attestation=attestation,
            attestation_bytes=attestation_bytes,
            fixture=fixture,
        )

    async def invoke_turn(
        self,
        context: ExecutionContext,
        *,
        adapter: AgentAdapter,
        target: AgentTarget,
        gate_a: GateAEvidence,
        binding_sha256: str,
        role: str,
        target_id: str,
        phase: str,
        operation: str,
        prompt: str,
        output_schema: dict[str, Any] | None,
        working_directory: Path,
        access_mode: str,
        failure_kind: FailureKind,
        session_id: str | None = None,
        peer_failure: asyncio.Event | None = None,
        workflow_timeout: asyncio.Event | None = None,
    ) -> TurnResult:
        outbound = prompt.encode("utf-8")
        persisted_prompt = context.credentials.redact_text(prompt)
        request_artifact = AgentRequestArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            role=role,
            target_id=target_id,
            turn_phase=phase,
            outbound_prompt_sha256=hashlib.sha256(outbound).hexdigest(),
            persisted_prompt_sha256=hashlib.sha256(
                persisted_prompt.encode("utf-8")
            ).hexdigest(),
            prompt=persisted_prompt,
            output_schema=output_schema,
            timeout_seconds=context.config.limits.agent_turn_seconds,
            access_mode=access_mode,
        )
        root = f"turns/{role}/{target_id}/{phase}"
        request_sha = context.service.store.write_artifact(
            context.handle, f"{root}.request.json", request_artifact
        )
        request = AgentRequest(
            role=role,
            target_id=target_id,
            turn_phase=phase,
            prompt=prompt,
            output_schema=output_schema,
            timeout_seconds=context.config.limits.agent_turn_seconds,
            working_directory=str(working_directory),
            access_mode=access_mode,
        )
        started = datetime.now(UTC)
        process_started = True
        termination = "completed"
        exit_code: int | None = None
        attempt_failure: FailureKind | None = None
        response: AgentResponse | None = None
        diagnostic: str | None = None
        caught: BaseException | None = None
        try:
            invocation = (
                adapter.start(request)
                if operation == "start"
                else adapter.resume(session_id or "", request)
            )
            raw_response = await asyncio.wait_for(
                invocation, timeout=context.config.limits.agent_turn_seconds
            )
            exit_code = 0
            try:
                response = AgentResponse.model_validate(raw_response)
                validate_model_bounds(
                    response.model_dump(mode="python"),
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                )
            except Exception as exc:
                raise TurnFailure(
                    failure_kind, "agent returned an invalid native response envelope"
                ) from exc
            if response.runtime != target.runtime or response.requested_model != target.model:
                raise TurnFailure(
                    failure_kind,
                    "agent response target does not match the immutable request",
                )
            response = redacted_response(response, context)
        except asyncio.TimeoutError as exc:
            termination = "timeout"
            attempt_failure = failure_kind
            diagnostic = "agent turn reached its individual timeout"
            caught = exc
        except asyncio.CancelledError as exc:
            if workflow_timeout is not None and workflow_timeout.is_set():
                termination = "timeout"
                attempt_failure = None
                diagnostic = "agent turn cancelled after workflow timeout"
            elif peer_failure is not None and peer_failure.is_set():
                termination = "peer-failure"
                attempt_failure = failure_kind
                diagnostic = "agent turn cancelled after peer failure"
            else:
                termination = "cancelled"
                attempt_failure = failure_kind
                diagnostic = "agent turn cancelled"
            caught = exc
        except ModelMismatchError as exc:
            termination = "completed"
            attempt_failure = "MODEL_MISMATCH"
            diagnostic = str(exc)
            caught = exc
        except AgentProcessError as exc:
            termination = "completed"
            exit_code = exc.exit_code
            attempt_failure = failure_kind
            diagnostic = str(exc)
            response = None
            caught = exc
        except NativeTurnError as exc:
            attempt_failure = exc.kind or failure_kind
            diagnostic = exc.detail
            response = None
            caught = exc
        except NativePreflightError as exc:
            termination = "launch-failed"
            process_started = False
            attempt_failure = failure_kind
            diagnostic = (
                "native turn preparation failed: "
                f"{bounded_preflight_diagnostic(exc)}"
            )
            response = None
            caught = exc
        except DialecticFailure as exc:
            termination = "launch-failed"
            process_started = False
            attempt_failure = exc.kind
            diagnostic = exc.detail
            caught = exc
        except TurnFailure as exc:
            termination = "completed"
            attempt_failure = exc.kind
            diagnostic = exc.detail
            response = None
            caught = exc
        except Exception as exc:
            termination = "launch-failed"
            process_started = False
            attempt_failure = failure_kind
            diagnostic = f"agent invocation failed: {type(exc).__name__}"
            response = None
            caught = exc

        evidence = take_native_invocation_evidence(adapter)
        stream_out = empty_stream(context.config.limits.max_agent_stdout_bytes)
        stream_err = empty_stream(context.config.limits.max_agent_stderr_bytes)
        completed = datetime.now(UTC)
        if evidence is None:
            process_origin = "spawned-for-attempt" if process_started else "none"
            process_lifecycle = gate_a.preflight.process_lifecycle
            process_unit_id = (
                process_unit_id_for(context.handle.run_id, role, target_id, phase)
                if process_started
                else None
            )
            process_exit_code = (
                exit_code if exit_code is not None else (-1 if process_started else None)
            )
            process_disposition = "closed" if process_started else "not-started"
            response_completed_at = completed if response is not None else None
            capture_completed_at = completed
            if caught is None:
                attempt_end_reason = "response-returned"
            elif not process_started:
                attempt_end_reason = termination
            elif termination in {"timeout", "cancelled", "peer-failure"}:
                attempt_end_reason = termination
            elif attempt_failure == "AGENT_OUTPUT_TOO_LARGE":
                attempt_end_reason = "output-limit"
            elif attempt_failure == "PROCESS_CLEANUP_FAILED":
                attempt_end_reason = "cleanup-failed"
            else:
                attempt_end_reason = "agent-failed"
        else:
            stream_out = evidence.stdout.result
            stream_err = evidence.stderr.result
            started = evidence.started_at
            process_origin = evidence.process_origin
            process_lifecycle = evidence.process_lifecycle
            process_unit_id = evidence.process_unit_id
            process_exit_code = evidence.process_exit_code
            process_disposition = evidence.process_disposition
            capture_completed_at = evidence.capture_completed_at
            response_completed_at = (
                evidence.response_completed_at if response is not None else None
            )
            attempt_end_reason = evidence.attempt_end_reason
            if isinstance(caught, asyncio.CancelledError) and process_origin == "none":
                attempt_end_reason = termination
            if caught is not None and attempt_end_reason == "response-returned":
                attempt_end_reason = "agent-failed"
            if evidence.failure_kind is not None:
                attempt_failure = evidence.failure_kind  # type: ignore[assignment]
            if evidence.bounded_diagnostic is not None and diagnostic is None:
                diagnostic = evidence.bounded_diagnostic
            if process_disposition == "cleanup-failed":
                attempt_failure = "PROCESS_CLEANUP_FAILED"
                attempt_end_reason = "cleanup-failed"
        context.service.store.write_artifact(
            context.handle,
            f"{root}.stdout.txt",
            evidence.stdout.persisted if evidence is not None else b"",
        )
        context.service.store.write_artifact(
            context.handle,
            f"{root}.stderr.txt",
            evidence.stderr.persisted if evidence is not None else b"",
        )
        attempt = TurnAttemptArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            role=role,
            target_id=target_id,
            turn_phase=phase,
            operation=operation,
            request_artifact_sha256=request_sha,
            target_preflight_artifact_sha256=gate_a.preflight_sha256,
            capability_binding_artifact_sha256=binding_sha256,
            started_at=started,
            response_completed_at=response_completed_at,
            capture_completed_at=capture_completed_at,
            process_origin=process_origin,
            process_lifecycle=process_lifecycle,
            process_unit_id=process_unit_id,
            process_exit_code=process_exit_code,
            attempt_end_reason=attempt_end_reason,
            failure_kind=attempt_failure,
            process_disposition=process_disposition,
            stdout=stream_out,
            stderr=stream_err,
            response=response,
            bounded_diagnostic=diagnostic,
        )
        context.service.store.write_artifact(
            context.handle, f"{root}.attempt.json", attempt
        )
        if (
            response is not None
            and access_mode == "packet-only"
            and context.config.research_mode == "live-web"
            and role in {"reviewer", "participant", "moderator"}
        ):
            persist_source_citations(
                context,
                role=role,  # type: ignore[arg-type]
                target_id=target_id,
                phase=phase,  # type: ignore[arg-type]
                response_text=response.text,
                captured_at=capture_completed_at,
            )
        if caught is not None:
            if isinstance(caught, asyncio.CancelledError):
                if attempt_failure == "PROCESS_CLEANUP_FAILED":
                    raise DialecticFailure(
                        "PROCESS_CLEANUP_FAILED",
                        diagnostic or "agent cleanup failed during cancellation",
                    ) from caught
                raise caught
            kind = attempt_failure or failure_kind
            raise DialecticFailure(kind, diagnostic or "agent turn failed") from caught
        assert response is not None
        return TurnResult(response=response, attempt=attempt)

    @staticmethod
    def validate_launch_evidence(
        context: ExecutionContext,
        *,
        gate_a: GateAEvidence,
        binding: CapabilityBindingArtifact,
        binding_sha256: str,
        binding_relative_path: str,
        dynamic_paths: Mapping[str, Path],
    ) -> None:
        store = context.service.store
        try:
            preflight_bytes = store.read_artifact(
                context.handle, gate_a.preflight_relative_path, 1_048_576
            )
            binding_bytes = store.read_artifact(
                context.handle, binding_relative_path, 1_048_576
            )
            attestation_bytes = store.read_capability_attestation(
                gate_a.preflight.capability_attestation_sha256
            )
        except Exception as exc:
            raise DialecticFailure(
                "PREFLIGHT_FAILED", "launch capability evidence is unreadable"
            ) from exc
        if (
            hashlib.sha256(preflight_bytes).hexdigest() != gate_a.preflight_sha256
            or hashlib.sha256(binding_bytes).hexdigest() != binding_sha256
            or attestation_bytes is None
            or hashlib.sha256(attestation_bytes).hexdigest()
            != gate_a.preflight.capability_attestation_sha256
        ):
            raise DialecticFailure(
                "PREFLIGHT_FAILED", "launch capability evidence changed after binding"
            )
        try:
            persisted_preflight = TargetPreflightArtifact.model_validate_json(
                preflight_bytes, strict=True
            )
            persisted_binding = CapabilityBindingArtifact.model_validate_json(
                binding_bytes, strict=True
            )
            persisted_attestation = CapabilityAttestationArtifact.model_validate_json(
                attestation_bytes,
                strict=True,
                context={"probe_ids": gate_a.fixture.probe_ids},
            )
        except Exception as exc:
            raise DialecticFailure(
                "PREFLIGHT_FAILED", "launch capability evidence is invalid"
            ) from exc
        if (
            persisted_preflight != gate_a.preflight
            or persisted_binding != binding
            or persisted_attestation != gate_a.attestation
            or binding.profile_template_sha256 != gate_a.fixture.template_sha256
            or binding.concrete_profile_sha256 == ""
        ):
            raise DialecticFailure(
                "PREFLIGHT_FAILED", "launch policy hashes no longer match Gate B"
            )
        try:
            validate_binding_identities(
                binding,
                dynamic_paths=dynamic_paths,
                platform_backend=gate_a.attestation.platform_backend,
            )
        except CapabilityEvidenceError as exc:
            raise DialecticFailure(
                "PREFLIGHT_FAILED", "launch filesystem identity changed after binding"
            ) from exc


def authorize_native_binding(
    adapter: AgentAdapter,
    binding: CapabilityBindingArtifact,
    concrete_profile: Mapping[str, Any],
    dynamic_paths: Mapping[str, Path],
) -> None:
    binder = getattr(adapter, "bind_capability", None)
    if callable(binder):
        binder(binding, concrete_profile, dynamic_paths)


def take_native_invocation_evidence(
    adapter: AgentAdapter,
) -> NativeInvocationEvidence | None:
    reader = getattr(adapter, "take_invocation_evidence", None)
    if not callable(reader):
        return None
    evidence = reader()
    if evidence is not None and not isinstance(evidence, NativeInvocationEvidence):
        raise DialecticFailure(
            "INTERNAL_ERROR", "native adapter returned invalid invocation evidence"
        )
    return evidence


def bounded_preflight_diagnostic(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        detail = "native preflight exceeded its configured timeout"
    elif isinstance(
        error,
        (NativePreflightError, NativeEnvelopeError, CapabilityEvidenceError),
    ):
        detail = str(error)
    else:
        detail = f"unexpected {type(error).__name__}"
    encoded = detail.encode("utf-8", errors="replace")[:1024]
    return encoded.decode("utf-8", errors="ignore") or "native preflight failed"


def concrete_profile(
    fixture: CapabilityFixture, dynamic_paths: Mapping[str, Path]
) -> dict[str, Any]:
    return instantiate_capability_template(fixture, dynamic_paths)


def empty_stream(limit: int) -> StreamCaptureResult:
    return StreamCaptureResult(
        configured_limit_bytes=limit,
        accepted_pre_redaction_bytes=0,
        accepted_pre_redaction_sha256=_EMPTY_SHA256,
        discarded_guard_bytes=0,
        discarded_guard_reason="none",
        truncated=False,
        persisted_bytes=0,
        persisted_sha256=_EMPTY_SHA256,
        triggered_termination=False,
    )


def process_unit_id_for(run_id: str, role: str, target_id: str, phase: str) -> str:
    digest = hashlib.sha256(
        f"{run_id}\0{role}\0{target_id}\0{phase}".encode("utf-8")
    ).digest()[:10]
    return base64.b32encode(digest).decode("ascii").lower()


def redacted_response(response: AgentResponse, context: ExecutionContext) -> AgentResponse:
    payload = response.model_dump(mode="python")

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            return context.credentials.redact_text(value)
        if isinstance(value, dict):
            return {key: redact(child) for key, child in value.items()}
        if isinstance(value, list):
            return [redact(child) for child in value]
        return value

    payload["text"] = context.credentials.redact_text(response.text)
    payload["structured_output"] = redact(response.structured_output)
    payload["usage"] = redact(response.usage)
    return AgentResponse.model_validate(payload)


def canonical_mapping_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def require_packet_bound(
    context: ExecutionContext,
    prompt: str,
    *,
    output_schema: dict[str, Any] | None,
    target_id: str,
) -> None:
    packet = {"prompt": prompt, "output_schema": output_schema}
    if len(canonical_mapping_bytes(packet)) > context.config.limits.max_packet_bytes:
        raise DialecticFailure(
            "PACKET_TOO_LARGE",
            f"outbound packet exceeds max_packet_bytes for {target_id}",
        )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def _platform_backend() -> str:
    if os.name == "nt":
        return "windows-scripted"
    if sys.platform.startswith("linux"):
        return "linux-scripted"
    return f"{sys.platform}-scripted"
