"""Versioned Grok Build ACP adapter and persistent participant lease."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping, NoReturn

from .capabilities import instantiate_capability_template
from .acp_transport import (
    AcpEpochCapture,
    AcpError,
    AcpLease,
    AcpLeaseFactory,
    AcpLogicalResponse,
    AcpProtocolError,
    AcpTurnTimeout,
    ManagedAcpLeaseFactory,
)
from .adapters import ModelMismatchError, verify_model_equivalence
from .contracts import (
    ARTIFACT_SCHEMA_VERSION,
    ResearchMode,
    TOOL_VERSION,
    SessionCloseReason,
)
from .json_schema import JsonSchemaError, validate_json_schema
from .launcher import build_launch_spec, validate_argv
from .native_adapters import (
    NativeAdapterBase,
    NativeEnvelopeError,
    NativeInvocationEvidence,
    NativePreflightError,
    NativeTurnError,
    _BoundProfile,
    _boolean_object_schema,
    _new_process_unit_id,
    _probe_bound_profile,
    _probe_results,
    _require_static_flags,
    _strict_utf8,
    _trusted_environment,
)
from .native_process import NativeProcessResult
from .output import OutputError, extract_json_payload, strict_json_loads
from .schemas import AgentRequest, AgentResponse

_GROK_VERSION_RE = re.compile(r"^(?:grok(?:-build)?\s+)?v?(?P<version>\d+\.\d+\.\d+)$", re.I)


@dataclass(slots=True)
class _PendingPersistentTurn:
    request: AgentRequest
    started_at: datetime
    response_completed_at: datetime
    response: AgentResponse
    origin: Literal["spawned-for-attempt", "retained-from-prior-turn"]


@dataclass(slots=True)
class _PersistentState:
    lease: AcpLease
    pending: _PendingPersistentTurn | None = None
    resume_prepared: bool = False
    prepared_request: AgentRequest | None = None


class GrokAdapter(NativeAdapterBase):
    runtime = "grok-build"

    def __init__(self, *args: Any, acp_factory: AcpLeaseFactory | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.acp_factory = acp_factory or ManagedAcpLeaseFactory()
        self._persistent: dict[str, _PersistentState] = {}
        self._closed_sessions: set[str] = set()
        self._inventory_verified = False

    def _version_arguments(self) -> tuple[str, ...]:
        return ("version",)

    def _help_arguments(self) -> tuple[tuple[str, ...], ...]:
        return (("--help",), ("agent", "stdio", "--help"), ("inspect", "--help"))

    def _authentication_arguments(self) -> tuple[str, ...]:
        return ("inspect", "--json")

    def _parse_version(self, output: str) -> str:
        match = _GROK_VERSION_RE.fullmatch(output.strip())
        if match is None:
            raise NativePreflightError("unrecognized Grok Build version output")
        return match.group("version")

    def _verify_help(self, output: str) -> None:
        required = [
            "agent",
            "stdio",
            "inspect",
            "--no-auto-update",
            "--no-memory",
            "--no-plan",
            "--no-subagents",
            "--safe-mode",
            "--tools",
        ]
        required.extend(
            ("--allow",) if self.research_mode == "live-web" else ("--disable-web-search",)
        )
        if any(value not in output for value in required):
            raise NativePreflightError("installed Grok Build lacks a required ACP control")

    def _verify_authentication(self, result: NativeProcessResult) -> None:
        value = strict_json_loads(_strict_utf8(result.stdout.persisted, "Grok inspect"))
        if not isinstance(value, dict):
            raise NativePreflightError("Grok inspect output is not an object")
        inventories = (
            _required_inventory(value, ("configSources", "config_sources", "sources")),
            _required_inventory(value, ("mcpServers", "mcp_servers")),
            _required_inventory(value, ("tools",)),
        )
        if any(inventory not in ({}, []) for inventory in inventories):
            raise NativePreflightError("Grok effective inventory is not capability-empty")
        self._inventory_verified = True

    def _verified_inventory_flags(self) -> tuple[str, ...]:
        if not self._inventory_verified:
            raise NativePreflightError("Grok effective inventory was not verified")
        return (
            "inspect:config-sources=0",
            "inspect:mcp-servers=0",
            "inspect:tools=0",
        )

    async def _runtime_preflight(
        self, executable: Path, environment: Mapping[str, str]
    ) -> None:
        fixture = self.fixture
        with tempfile.TemporaryDirectory(prefix="dialectic-grok-preflight-") as directory:
            arguments = _grok_arguments(
                self.target.model, self.target.effort, self.research_mode
            )
            _require_static_flags(arguments, fixture.static_flags)
            plan = build_launch_spec(executable, arguments)
            lease = await self.acp_factory.open(
                plan,
                cwd=Path(directory),
                environment=environment,
                model=self.target.model,
                process_unit_id=_new_process_unit_id(),
                stdout_limit=self.stdout_limit,
                stderr_limit=self.stderr_limit,
                credentials=self.credentials,
                preflight_seconds=self.preflight_seconds,
            )
            final = await lease.close(self.graceful_kill_seconds)
            if not final.cleanup_confirmed:
                raise NativePreflightError("Grok ACP authentication cleanup failed")
        if fixture.prompt_transport != "acp-stdio":
            raise NativePreflightError("Grok fixture does not select ACP stdio")

    async def _native_capability_probe(self, fixture):  # type: ignore[no-untyped-def]
        with tempfile.TemporaryDirectory(prefix="dialectic-grok-probe-") as directory:
            neutral = Path(directory).resolve(strict=True)
            dynamic_paths = {"neutral_role_dir": neutral}
            concrete = instantiate_capability_template(
                fixture.capability_fixture, dynamic_paths
            )
            bound = _probe_bound_profile(
                self, fixture, concrete, dynamic_paths
            )
            executable = Path(
                self.which(fixture.executable_name) or fixture.executable_name
            ).resolve(strict=True)
            environment = self._turn_environment(
                _trusted_environment(
                    fixture, self.source_environment, self.credentials
                )[0],
                bound,
            )
            arguments = _grok_arguments(
                self.target.model, self.target.effort, self.research_mode
            )
            _require_static_flags(arguments, fixture.static_flags)
            lease = await self.acp_factory.open(
                build_launch_spec(executable, arguments),
                cwd=neutral,
                environment=environment,
                model=self.target.model,
                process_unit_id=_new_process_unit_id(),
                stdout_limit=self.stdout_limit,
                stderr_limit=self.stderr_limit,
                credentials=self.credentials,
                preflight_seconds=self.preflight_seconds,
            )
            schema = (
                _boolean_object_schema(
                    "empty_capabilities", "mcp_source", "web_search", "web_fetch",
                    "other_built_in_tools",
                )
                if self.research_mode == "live-web"
                else _boolean_object_schema(
                    "empty_capabilities", "mcp_source", "built_in_tools"
                )
            )
            request = AgentRequest(
                role=self.role,
                target_id="capability-probe",
                turn_phase={
                    "reviewer": "review",
                    "participant": "opening",
                    "moderator": "moderation",
                }[self.role],
                prompt=(
                    (
                        "This is a bounded ACP capability probe. Use WebSearch to find the "
                        "Example Domain site and WebFetch to retrieve https://example.com. "
                        "Report true only when each named web tool actually succeeds. Also "
                        "report whether the ACP client advertises any capability, whether any "
                        "MCP source exists, and whether any other built-in tool is available. "
                        "Return only the exact JSON schema."
                    )
                    if self.research_mode == "live-web"
                    else (
                        "This is a bounded ACP capability probe. Report whether the ACP client "
                        "advertises an empty capability set, whether any MCP source is available, "
                        "and whether any built-in tool is available. Return only the exact JSON "
                        "schema."
                    )
                ),
                output_schema=schema,
                timeout_seconds=self.capability_probe_seconds,
                working_directory=str(neutral),
                access_mode=self.access_mode,
            )
            try:
                logical = await lease.prompt(
                    request.prompt, self.capability_probe_seconds
                )
                response = self._normalize(logical, request)
            finally:
                epoch = await lease.close(self.graceful_kill_seconds)
            if not epoch.cleanup_confirmed:
                raise NativePreflightError("pinned Grok capability probe cleanup failed")
            value = response.structured_output
            if not isinstance(value, dict):
                raise NativePreflightError("Grok capability probe lacked structured output")
            observed = {
                "empty-capabilities-allow": value.get("empty_capabilities") is True,
                "mcp-source-deny": value.get("mcp_source") is False,
                **(
                    {
                        "web-search-allow": value.get("web_search") is True,
                        "web-fetch-allow": value.get("web_fetch") is True,
                        "other-tools-deny": value.get("other_built_in_tools") is False,
                    }
                    if self.research_mode == "live-web"
                    else {"built-in-tools-deny": value.get("built_in_tools") is False}
                ),
            }
            return _probe_results(fixture, observed, "pinned native Grok ACP probe")

    def _turn_arguments(
        self,
        operation: Literal["start", "resume"],
        session_id: str | None,
        request: AgentRequest,
        bound: _BoundProfile,
    ) -> tuple[str, ...]:
        return _grok_arguments(
            self.target.model, self.target.effort, self.research_mode
        )

    def _parse_envelope(self, stdout: str, request: AgentRequest) -> Any:
        raise RuntimeError("Grok ACP envelopes are parsed by the persistent transport")

    async def start(self, request: AgentRequest) -> AgentResponse:
        if request.access_mode != self.access_mode:
            raise NativePreflightError("request access mode mismatches adapter")
        if self.preflight_material().process_lifecycle == "persistent-acp-session":
            if request.role != "participant" or request.turn_phase != "opening":
                raise NativePreflightError("persistent Grok start requires an opening participant")
            return await self._start_persistent(request)
        return await self._run_per_turn(request)

    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse:
        if self.preflight_material().process_lifecycle != "persistent-acp-session":
            raise RuntimeError("per-turn Grok roles do not support native resume")
        state = self._persistent.get(session_id)
        if state is None or state.lease.session_id != session_id:
            raise NativeEnvelopeError("Grok ACP session lease is absent or mismatched")
        if not state.resume_prepared or self._last_invocation is not None:
            raise NativePreflightError(
                "persistent epoch evidence must be consumed before the next prompt"
            )
        if request.turn_phase not in {"cross-examination", "ballot"}:
            raise NativePreflightError("Grok resume has an invalid council phase")
        if state.prepared_request != request:
            raise NativePreflightError("Grok resume request mismatches the prepared epoch")
        state.resume_prepared = False
        state.prepared_request = None
        started = datetime.now(UTC)
        try:
            logical = await state.lease.prompt(request.prompt, request.timeout_seconds)
            response_completed = datetime.now(UTC)
            response = self._normalize(logical, request)
        except BaseException as exc:
            epoch = await self._close_failed_prompt(
                session_id, state, request, started, exc
            )
            _raise_grok_turn_failure(exc, epoch, "Grok retained prompt failed")
        state.pending = _PendingPersistentTurn(
            request=request,
            started_at=started,
            response_completed_at=response_completed,
            response=response,
            origin="retained-from-prior-turn",
        )
        return response

    async def prepare_resume(
        self, session_id: str, next_request: AgentRequest
    ) -> NativeInvocationEvidence:
        """Finalize one retained epoch before its successor prompt is admitted."""

        state = self._persistent.get(session_id)
        if state is None or state.pending is None or state.resume_prepared:
            raise NativePreflightError("Grok retained epoch is not ready for finalization")
        if self._last_invocation is not None:
            raise NativePreflightError("prior native invocation evidence is still unconsumed")
        expected_phase = {
            "opening": "cross-examination",
            "cross-examination": "ballot",
        }.get(state.pending.request.turn_phase)
        if (
            next_request.turn_phase != expected_phase
            or next_request.role != "participant"
            or next_request.target_id != state.pending.request.target_id
            or next_request.access_mode != self.access_mode
        ):
            raise NativePreflightError("Grok next request does not match the epoch boundary")
        try:
            epoch = await state.lease.switch_epoch()
        except BaseException as exc:
            self._persistent.pop(session_id, None)
            self._closed_sessions.add(session_id)
            final = await state.lease.close(self.graceful_kill_seconds)
            self._last_invocation = _failed_open_evidence(
                state.lease,
                state.pending.started_at,
                final,
                type(exc).__name__,
                turn_phase=state.pending.request.turn_phase,
                timeout=isinstance(exc, AcpTurnTimeout),
                cancelled=isinstance(exc, asyncio.CancelledError),
            )
            _raise_grok_turn_failure(
                exc, final, "Grok retained session failed before the next prompt"
            )
        evidence = _retained_evidence(state.lease, state.pending, epoch)
        state.pending = None
        state.resume_prepared = True
        state.prepared_request = next_request
        self._last_invocation = evidence
        return evidence

    async def close_retained_session(
        self, session_id: str, reason: SessionCloseReason
    ) -> None:
        if session_id in self._closed_sessions:
            raise RuntimeError("Grok retained session cleanup was already requested")
        state = self._persistent.get(session_id)
        if state is None:
            raise RuntimeError("Grok retained session lease is not owned by this adapter")
        if state.prepared_request is not None and reason == "completed":
            raise NativePreflightError(
                "a prepared retained epoch cannot close as an ordinary completion"
            )
        self._persistent.pop(session_id)
        self._closed_sessions.add(session_id)
        epoch = await state.lease.close(self.graceful_kill_seconds)
        if state.pending is not None:
            self._last_invocation = _closed_evidence(
                state.lease, state.pending, epoch, reason=reason
            )
        elif state.prepared_request is not None:
            self._last_invocation = _aborted_retained_evidence(
                state.lease, state.prepared_request, epoch, reason=reason
            )
        if not epoch.cleanup_confirmed:
            raise NativeTurnError(
                "PROCESS_CLEANUP_FAILED", "Grok retained session cleanup failed"
            )
        if epoch.stdout.result.truncated or epoch.stderr.result.truncated:
            raise NativeTurnError(
                "AGENT_OUTPUT_TOO_LARGE",
                "Grok retained final capture exceeded its output bound",
            )

    async def _start_persistent(self, request: AgentRequest) -> AgentResponse:
        started = datetime.now(UTC)
        unit_id = _new_process_unit_id()
        lease: AcpLease | None = None
        try:
            lease = await self._open_lease(request, unit_id)
            logical = await lease.prompt(request.prompt, request.timeout_seconds)
            response_completed = datetime.now(UTC)
            response = self._normalize(logical, request)
        except BaseException as exc:
            epoch: AcpEpochCapture | None = None
            if lease is None:
                self._record_acp_launch_failure(started, exc)
            else:
                epoch = await lease.close(self.graceful_kill_seconds)
                self._last_invocation = _failed_open_evidence(
                    lease,
                    started,
                    epoch,
                    type(exc).__name__,
                    turn_phase=request.turn_phase,
                    timeout=isinstance(exc, AcpTurnTimeout),
                    cancelled=isinstance(exc, asyncio.CancelledError),
                )
            _raise_grok_turn_failure(exc, epoch, "Grok persistent start failed")
        if lease.session_id in self._persistent or lease.session_id in self._closed_sessions:
            epoch = await lease.close(self.graceful_kill_seconds)
            self._last_invocation = _failed_open_evidence(
                lease, started, epoch, "ACP session id was reused"
            )
            _raise_grok_turn_failure(
                NativeEnvelopeError("Grok ACP returned a reused session id"),
                epoch,
                "Grok ACP returned a reused session id",
            )
        self._persistent[lease.session_id] = _PersistentState(
            lease=lease,
            pending=_PendingPersistentTurn(
                request=request,
                started_at=started,
                response_completed_at=response_completed,
                response=response,
                origin="spawned-for-attempt",
            ),
        )
        return response

    async def _run_per_turn(self, request: AgentRequest) -> AgentResponse:
        started = datetime.now(UTC)
        unit_id = _new_process_unit_id()
        lease: AcpLease | None = None
        epoch: AcpEpochCapture | None = None
        try:
            lease = await self._open_lease(request, unit_id)
            logical = await lease.prompt(request.prompt, request.timeout_seconds)
            response_completed = datetime.now(UTC)
            response = self._normalize(logical, request)
            epoch = await lease.close(self.graceful_kill_seconds)
            pending = _PendingPersistentTurn(
                request=request,
                started_at=started,
                response_completed_at=response_completed,
                response=response,
                origin="spawned-for-attempt",
            )
            self._last_invocation = _closed_evidence(
                lease, pending, epoch, reason="completed"
            )
            if not epoch.cleanup_confirmed:
                raise NativeTurnError(
                    "PROCESS_CLEANUP_FAILED", "Grok per-turn ACP cleanup failed"
                )
            if epoch.stdout.result.truncated or epoch.stderr.result.truncated:
                raise NativeTurnError(
                    "AGENT_OUTPUT_TOO_LARGE",
                    "Grok per-turn final capture exceeded its output bound",
                )
            return response
        except BaseException as exc:
            if lease is None:
                self._record_acp_launch_failure(started, exc)
            elif self._last_invocation is None:
                epoch = await lease.close(self.graceful_kill_seconds)
                self._last_invocation = _failed_open_evidence(
                    lease, started, epoch, type(exc).__name__
                )
            _raise_grok_turn_failure(exc, epoch, "Grok per-turn prompt failed")

    async def _open_lease(self, request: AgentRequest, unit_id: str) -> AcpLease:
        bound = self._bound_profile(request)
        arguments = _grok_arguments(
            self.target.model, self.target.effort, self.research_mode
        )
        validate_argv(arguments)
        _require_static_flags(arguments, self.fixture.static_flags)
        material = self.preflight_material()
        self._revalidate_material(material)
        return await self.acp_factory.open(
            build_launch_spec(material.resolved_executable, arguments),
            cwd=Path(request.working_directory),
            environment=self._turn_environment(material.trusted_environment, bound),
            model=self.target.model,
            process_unit_id=unit_id,
            stdout_limit=self.stdout_limit,
            stderr_limit=self.stderr_limit,
            credentials=self.credentials,
            preflight_seconds=self.preflight_seconds,
        )

    def _normalize(
        self, logical: AcpLogicalResponse, request: AgentRequest
    ) -> AgentResponse:
        try:
            verify_model_equivalence(
                requested=self.target.model,
                resolved=self.canonical_aliases.get(self.target.model),
                actual=logical.actual_model,
                aliases=self.canonical_aliases,
            )
            structured: dict[str, Any] | None = None
            text = logical.text
            if request.output_schema is not None:
                payload = extract_json_payload(text)
                if not isinstance(payload, dict):
                    raise NativeEnvelopeError("Grok structured response is not an object")
                validate_json_schema(payload, request.output_schema)
                structured = payload
                text = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            return AgentResponse(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                runtime=self.runtime,
                requested_model=self.target.model,
                resolved_requested_model=self.canonical_aliases.get(
                    self.target.model, self.target.model
                ),
                actual_model=logical.actual_model,
                session_id=logical.session_id,
                text=text,
                structured_output=structured,
                usage=logical.usage,
            )
        except (OutputError, JsonSchemaError, ModelMismatchError) as exc:
            raise NativeEnvelopeError("Grok ACP response failed deterministic validation") from exc

    async def _close_failed_prompt(
        self,
        session_id: str,
        state: _PersistentState,
        request: AgentRequest,
        started: datetime,
        error: BaseException,
    ) -> AcpEpochCapture:
        self._persistent.pop(session_id, None)
        self._closed_sessions.add(session_id)
        epoch = await state.lease.close(self.graceful_kill_seconds)
        self._last_invocation = _failed_open_evidence(
            state.lease,
            started,
            epoch,
            type(error).__name__,
            turn_phase=request.turn_phase,
            timeout=isinstance(error, AcpTurnTimeout),
            cancelled=isinstance(error, asyncio.CancelledError),
        )
        return epoch

    def _record_acp_launch_failure(self, started: datetime, error: BaseException) -> None:
        self._record_launch_failure(started, error)


def _grok_arguments(
    model: str, effort: str | None, research_mode: ResearchMode = "offline"
) -> tuple[str, ...]:
    arguments = [
        "--no-auto-update",
        "--no-memory",
    ]
    if research_mode == "live-web":
        arguments.extend(
            (
                "--tools", "WebSearch,WebFetch",
                "--allow", "WebSearch",
                "--allow", "WebFetch",
            )
        )
    else:
        arguments.extend(("--disable-web-search", "--tools", ""))
    arguments.extend(("--no-plan", "--no-subagents", "--safe-mode", "--model", model))
    if effort is not None:
        arguments.extend(("--effort", effort))
    arguments.extend(("agent", "stdio"))
    return tuple(arguments)


def _required_inventory(
    value: Mapping[str, Any], aliases: tuple[str, ...]
) -> object:
    present = [key for key in aliases if key in value]
    if len(present) != 1:
        raise NativePreflightError(
            f"Grok inspect omitted or duplicated inventory {aliases[0]}"
        )
    inventory = value[present[0]]
    if not isinstance(inventory, (dict, list)):
        raise NativePreflightError(
            f"Grok inspect inventory {present[0]} has an invalid shape"
        )
    return inventory


def _raise_grok_turn_failure(
    error: BaseException,
    epoch: AcpEpochCapture | None,
    diagnostic: str,
) -> NoReturn:
    if epoch is not None and not epoch.cleanup_confirmed:
        raise NativeTurnError(
            "PROCESS_CLEANUP_FAILED", f"{diagnostic}; cleanup could not be proved"
        ) from error
    if epoch is not None and (
        epoch.stdout.result.truncated or epoch.stderr.result.truncated
    ):
        raise NativeTurnError(
            "AGENT_OUTPUT_TOO_LARGE", f"{diagnostic}; output limit exceeded"
        ) from error
    if isinstance(error, asyncio.CancelledError):
        raise error
    if isinstance(error, AcpTurnTimeout):
        raise NativeTurnError(None, f"{diagnostic}; response deadline reached") from error
    if isinstance(error, NativeEnvelopeError):
        raise error
    if isinstance(error, AcpError):
        raise NativeEnvelopeError(diagnostic) from error
    raise error


def _retained_evidence(
    lease: AcpLease,
    pending: _PendingPersistentTurn,
    epoch: AcpEpochCapture,
) -> NativeInvocationEvidence:
    return NativeInvocationEvidence(
        started_at=pending.started_at,
        response_completed_at=pending.response_completed_at,
        capture_completed_at=datetime.now(UTC),
        process_origin=pending.origin,
        process_lifecycle="persistent-acp-session",
        process_unit_id=lease.process_unit_id,
        process_exit_code=None,
        attempt_end_reason="response-returned",
        failure_kind=None,
        process_disposition="retained-for-session",
        stdout=epoch.stdout,
        stderr=epoch.stderr,
    )


def _closed_evidence(
    lease: AcpLease,
    pending: _PendingPersistentTurn,
    epoch: AcpEpochCapture,
    *,
    reason: SessionCloseReason,
) -> NativeInvocationEvidence:
    cleanup_failed = not epoch.cleanup_confirmed
    output_limit = epoch.stdout.result.truncated or epoch.stderr.result.truncated
    if cleanup_failed:
        end_reason = "cleanup-failed"
    elif output_limit:
        end_reason = "output-limit"
    elif reason == "completed":
        end_reason = "response-returned"
    else:
        end_reason = {
            "phase-failure": "peer-failure",
            "workflow-timeout": "timeout",
            "cancelled": "cancelled",
        }[reason]
    return NativeInvocationEvidence(
        started_at=pending.started_at,
        response_completed_at=pending.response_completed_at,
        capture_completed_at=datetime.now(UTC),
        process_origin=pending.origin,
        process_lifecycle="persistent-acp-session"
        if pending.request.role == "participant"
        else "per-turn",
        process_unit_id=lease.process_unit_id,
        process_exit_code=epoch.process_exit_code,
        attempt_end_reason=end_reason,
        failure_kind=(
            "PROCESS_CLEANUP_FAILED"
            if cleanup_failed
            else "AGENT_OUTPUT_TOO_LARGE"
            if output_limit
            else None
        ),
        process_disposition="cleanup-failed" if cleanup_failed else "closed",
        stdout=epoch.stdout,
        stderr=epoch.stderr,
        bounded_diagnostic=(
            "ACP process-unit cleanup failed"
            if cleanup_failed
            else "ACP final capture exceeded its output bound"
            if output_limit
            else None
        ),
    )


def _failed_open_evidence(
    lease: AcpLease,
    started: datetime,
    epoch: AcpEpochCapture,
    diagnostic: str,
    *,
    turn_phase: str | None = None,
    timeout: bool = False,
    cancelled: bool = False,
) -> NativeInvocationEvidence:
    cleanup_failed = not epoch.cleanup_confirmed
    output_limit = epoch.stdout.result.truncated or epoch.stderr.result.truncated
    return NativeInvocationEvidence(
        started_at=started,
        response_completed_at=None,
        capture_completed_at=datetime.now(UTC),
        process_origin="spawned-for-attempt"
        if turn_phase in {None, "opening", "review", "moderation"}
        else "retained-from-prior-turn",
        process_lifecycle="persistent-acp-session"
        if turn_phase in {"opening", "cross-examination", "ballot"}
        else "per-turn",
        process_unit_id=lease.process_unit_id,
        process_exit_code=epoch.process_exit_code,
        attempt_end_reason=(
            "cleanup-failed"
            if cleanup_failed
            else "output-limit"
            if output_limit
            else "timeout"
            if timeout
            else "cancelled"
            if cancelled
            else "agent-failed"
        ),
        failure_kind=(
            "PROCESS_CLEANUP_FAILED"
            if cleanup_failed
            else "AGENT_OUTPUT_TOO_LARGE" if output_limit else None
        ),
        process_disposition="cleanup-failed" if cleanup_failed else "closed",
        stdout=epoch.stdout,
        stderr=epoch.stderr,
        bounded_diagnostic=f"Grok ACP turn failed: {diagnostic}",
    )


def _aborted_retained_evidence(
    lease: AcpLease,
    request: AgentRequest,
    epoch: AcpEpochCapture,
    *,
    reason: SessionCloseReason,
) -> NativeInvocationEvidence:
    cleanup_failed = not epoch.cleanup_confirmed
    output_limit = epoch.stdout.result.truncated or epoch.stderr.result.truncated
    end_reason = {
        "phase-failure": "peer-failure",
        "workflow-timeout": "timeout",
        "cancelled": "cancelled",
    }[reason]
    return NativeInvocationEvidence(
        started_at=datetime.now(UTC),
        response_completed_at=None,
        capture_completed_at=datetime.now(UTC),
        process_origin="retained-from-prior-turn",
        process_lifecycle="persistent-acp-session",
        process_unit_id=lease.process_unit_id,
        process_exit_code=epoch.process_exit_code,
        attempt_end_reason=(
            "cleanup-failed"
            if cleanup_failed
            else "output-limit" if output_limit else end_reason
        ),
        failure_kind=(
            "PROCESS_CLEANUP_FAILED"
            if cleanup_failed
            else "AGENT_OUTPUT_TOO_LARGE" if output_limit else None
        ),
        process_disposition="cleanup-failed" if cleanup_failed else "closed",
        stdout=epoch.stdout,
        stderr=epoch.stderr,
        bounded_diagnostic=(
            "Grok retained epoch cleanup failed"
            if cleanup_failed
            else f"Grok retained epoch closed before {request.turn_phase}: {reason}"
        ),
    )
