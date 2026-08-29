"""Transport-neutral adapter protocol and deterministic scripted test adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, Literal, Protocol

from .schemas import (
    AgentRequest,
    AgentResponse,
    AgentTarget,
    DialecticConfig,
    PreflightResult,
)
from .contracts import SessionCloseReason

SCRIPTED_STDOUT_LIMIT_BYTES = 8_388_608
SCRIPTED_STDERR_LIMIT_BYTES = 2_097_152


class AgentAdapter(Protocol):
    process_local_continuation: bool
    prompt_transport: Literal["stdin", "acp-stdio"]

    async def preflight(self, target: AgentTarget) -> PreflightResult: ...

    async def start(self, request: AgentRequest) -> AgentResponse: ...

    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse: ...

    async def close_retained_session(
        self, session_id: str, reason: SessionCloseReason
    ) -> None: ...


class ModelMismatchError(RuntimeError):
    pass


class AgentProcessError(RuntimeError):
    """A native process started and returned a nonzero exit code."""

    def __init__(self, exit_code: int, detail: str) -> None:
        if exit_code == 0:
            raise ValueError("AgentProcessError requires a nonzero exit code")
        self.exit_code = exit_code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ScriptedInvocation:
    operation: str
    session_id: str | None
    prompt_sha256: str
    prompt: str
    role: str
    target_id: str
    turn_phase: str
    started_at: datetime
    completed_at: datetime
    requested_schema: dict | None


@dataclass(slots=True)
class ScriptedStep:
    response: AgentResponse | None = None
    delay_seconds: float = 0.0
    error: Exception | None = None
    callback: Callable[[AgentRequest], Awaitable[None] | None] | None = None


class ScriptedAgentAdapter:
    def __init__(
        self,
        target: AgentTarget,
        steps: list[ScriptedStep],
        *,
        canonical_aliases: dict[str, str] | None = None,
        persistent_session: bool = False,
        close_error: Exception | None = None,
        stdout_limit: int = SCRIPTED_STDOUT_LIMIT_BYTES,
        stderr_limit: int = SCRIPTED_STDERR_LIMIT_BYTES,
    ) -> None:
        if persistent_session and target.runtime != "grok-build":
            raise ValueError("only the scripted Grok fixture may retain a session")
        self.target = target
        self._steps = deque(steps)
        self.canonical_aliases = canonical_aliases or {}
        self.process_local_continuation = persistent_session
        self.prompt_transport = (
            "acp-stdio" if target.runtime == "grok-build" else "stdin"
        )
        self._close_error = close_error
        self._stdout_limit = stdout_limit
        self._stderr_limit = stderr_limit
        self._leased_session_id: str | None = None
        self._process_unit_id: str | None = None
        self._pending: tuple[ScriptedInvocation, AgentResponse] | None = None
        self._prepared_request: AgentRequest | None = None
        self._last_invocation: object | None = None
        self.close_count = 0
        self.invocations: list[ScriptedInvocation] = []

    async def preflight(self, target: AgentTarget) -> PreflightResult:
        if target != self.target:
            raise ValueError("scripted adapter target mismatch")
        return PreflightResult(
            target=target,
            requested_model=target.model,
            resolved_requested_model=self.canonical_aliases.get(target.model, target.model),
            actual_model=None,
            authentication_verified=True,
        )

    async def start(self, request: AgentRequest) -> AgentResponse:
        response = await self._invoke("start", None, request)
        if self.process_local_continuation:
            if request.role != "participant" or request.turn_phase != "opening":
                raise RuntimeError("persistent scripted start requires an opening participant")
            if response.session_id is None:
                self._process_unit_id = base64.b32encode(
                    secrets.token_bytes(10)
                ).decode("ascii").lower()
                self._last_invocation = self._scripted_evidence(
                    self.invocations[-1],
                    response_returned=True,
                    disposition="closed",
                    exit_code=0,
                )
                return response
            self._leased_session_id = response.session_id
            self._process_unit_id = base64.b32encode(
                secrets.token_bytes(10)
            ).decode("ascii").lower()
            self._pending = (self.invocations[-1], response)
        return response

    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse:
        if self.process_local_continuation:
            if session_id != self._leased_session_id:
                raise RuntimeError("scripted persistent session is absent or mismatched")
            if self._prepared_request != request or self._last_invocation is not None:
                raise RuntimeError("scripted persistent epoch was not prepared")
            self._prepared_request = None
        try:
            response = await self._invoke("resume", session_id, request)
        except BaseException:
            if self.process_local_continuation:
                self._prepared_request = request
            raise
        if self.process_local_continuation:
            self._pending = (self.invocations[-1], response)
        return response

    async def prepare_resume(
        self, session_id: str, next_request: AgentRequest
    ) -> object:
        if not self.process_local_continuation or session_id != self._leased_session_id:
            raise RuntimeError("scripted retained session is unavailable")
        if self._pending is None or self._prepared_request is not None:
            raise RuntimeError("scripted retained epoch is not ready")
        invocation, _response = self._pending
        expected = {
            "opening": "cross-examination",
            "cross-examination": "ballot",
        }.get(invocation.turn_phase)
        if next_request.turn_phase != expected:
            raise RuntimeError("scripted retained epoch has an invalid successor")
        evidence = self._scripted_evidence(
            invocation,
            response_returned=True,
            disposition="retained-for-session",
            exit_code=None,
        )
        self._pending = None
        self._prepared_request = next_request
        self._last_invocation = evidence
        return evidence

    async def close_retained_session(
        self, session_id: str, reason: SessionCloseReason
    ) -> None:
        if not self.process_local_continuation:
            raise RuntimeError("scripted per-turn adapter has no retained session lease")
        if session_id != self._leased_session_id:
            raise RuntimeError("scripted retained session is not owned")
        self.close_count += 1
        source = self._pending
        response_returned = source is not None
        if source is None:
            if self._prepared_request is None:
                raise RuntimeError("scripted retained session has no active epoch")
            now = datetime.now(UTC)
            invocation = ScriptedInvocation(
                operation="resume",
                session_id=session_id,
                prompt_sha256=hashlib.sha256(
                    self._prepared_request.prompt.encode("utf-8")
                ).hexdigest(),
                prompt=self._prepared_request.prompt,
                role=self._prepared_request.role,
                target_id=self._prepared_request.target_id,
                turn_phase=self._prepared_request.turn_phase,
                started_at=now,
                completed_at=now,
                requested_schema=self._prepared_request.output_schema,
            )
        else:
            invocation = source[0]
        cleanup_failed = self._close_error is not None
        self._last_invocation = self._scripted_evidence(
            invocation,
            response_returned=response_returned,
            disposition="cleanup-failed" if cleanup_failed else "closed",
            exit_code=None if cleanup_failed else 0,
            reason=reason,
        )
        self._pending = None
        self._prepared_request = None
        self._leased_session_id = None
        if self._close_error is not None:
            raise self._close_error

    def take_invocation_evidence(self) -> object | None:
        evidence = self._last_invocation
        self._last_invocation = None
        return evidence

    def _scripted_evidence(
        self,
        invocation: ScriptedInvocation,
        *,
        response_returned: bool,
        disposition: str,
        exit_code: int | None,
        reason: SessionCloseReason = "completed",
    ) -> object:
        # Imported lazily to keep the adapter protocol module free of a cycle.
        from .native_adapters import NativeInvocationEvidence, _empty_capture
        from .redaction import KnownCredentials

        if disposition == "cleanup-failed":
            end_reason = "cleanup-failed"
            failure_kind = "PROCESS_CLEANUP_FAILED"
        elif response_returned and reason == "completed":
            end_reason = "response-returned"
            failure_kind = None
        else:
            end_reason = {
                "phase-failure": "peer-failure",
                "workflow-timeout": "timeout",
                "cancelled": "cancelled",
                "completed": "response-returned",
            }[reason]
            failure_kind = None
        return NativeInvocationEvidence(
            started_at=invocation.started_at,
            response_completed_at=invocation.completed_at if response_returned else None,
            capture_completed_at=datetime.now(UTC),
            process_origin=(
                "spawned-for-attempt"
                if invocation.operation == "start"
                else "retained-from-prior-turn"
            ),
            process_lifecycle="persistent-acp-session",
            process_unit_id=self._process_unit_id,
            process_exit_code=exit_code,
            attempt_end_reason=end_reason,
            failure_kind=failure_kind,
            process_disposition=disposition,
            stdout=_empty_capture(self._stdout_limit, KnownCredentials()),
            stderr=_empty_capture(self._stderr_limit, KnownCredentials()),
            bounded_diagnostic=(
                "scripted retained cleanup failed" if disposition == "cleanup-failed" else None
            ),
        )

    async def _invoke(
        self, operation: str, session_id: str | None, request: AgentRequest
    ) -> AgentResponse:
        if not self._steps:
            raise RuntimeError("scripted adapter has no queued response")
        step = self._steps.popleft()
        started = datetime.now(UTC)
        try:
            if step.callback is not None:
                callback_result = step.callback(request)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            if step.delay_seconds:
                await asyncio.sleep(step.delay_seconds)
            if step.error is not None:
                raise step.error
            if step.response is None:
                raise RuntimeError("scripted step has neither response nor error")
            verify_model_equivalence(
                requested=self.target.model,
                resolved=self.canonical_aliases.get(self.target.model),
                actual=step.response.actual_model,
                aliases=self.canonical_aliases,
            )
            return step.response
        finally:
            self.invocations.append(
                ScriptedInvocation(
                    operation=operation,
                    session_id=session_id,
                    prompt_sha256=hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
                    prompt=request.prompt,
                    role=request.role,
                    target_id=request.target_id,
                    turn_phase=request.turn_phase,
                    started_at=started,
                    completed_at=datetime.now(UTC),
                    requested_schema=request.output_schema,
                )
            )


def verify_model_equivalence(
    *,
    requested: str,
    resolved: str | None,
    actual: str | None,
    aliases: dict[str, str],
) -> None:
    if actual is None:
        return
    expected = resolved or aliases.get(requested, requested)
    canonical_actual = aliases.get(actual, actual)
    if canonical_actual != expected:
        raise ModelMismatchError(
            f"actual model {actual} is not equivalent to requested model {requested}"
        )


class AgentRegistry:
    """Resolve active-mode targets without touching unused configuration sections."""

    @staticmethod
    def code_targets(config: DialecticConfig) -> tuple[AgentTarget, list[tuple[str, AgentTarget]]]:
        if config.driver is None or config.reviewers is None:
            raise ValueError("code mode requires driver and reviewers")
        driver = AgentTarget(**config.driver.model_dump())
        reviewers: list[tuple[str, AgentTarget]] = []
        for reviewer in config.reviewers:
            target = (
                driver
                if reviewer.target == "@driver"
                else AgentTarget(
                    runtime=reviewer.runtime,
                    model=reviewer.model,
                    effort=reviewer.effort,
                )
            )
            reviewers.append((reviewer.id, target))
        return driver, reviewers

    @staticmethod
    def council_targets(config: DialecticConfig) -> tuple[list[tuple[str, AgentTarget]], AgentTarget]:
        if config.council is None:
            raise ValueError("council mode requires council")
        participants = [
            (participant.id, AgentTarget(**participant.model_dump(exclude={"id"})))
            for participant in config.council.participants
        ]
        moderator = AgentTarget(**config.council.moderator.model_dump())
        return participants, moderator
