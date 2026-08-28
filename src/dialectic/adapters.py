"""Transport-neutral adapter protocol and deterministic scripted test adapter."""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, Protocol

from .schemas import (
    AgentRequest,
    AgentResponse,
    AgentTarget,
    DialecticConfig,
    PreflightResult,
)


class AgentAdapter(Protocol):
    async def preflight(self, target: AgentTarget) -> PreflightResult: ...

    async def start(self, request: AgentRequest) -> AgentResponse: ...

    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse: ...


class ModelMismatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ScriptedInvocation:
    operation: str
    session_id: str | None
    prompt_sha256: str
    prompt: str
    role: str
    target_id: str
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
    ) -> None:
        self.target = target
        self._steps = deque(steps)
        self.canonical_aliases = canonical_aliases or {}
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
        return await self._invoke("start", None, request)

    async def resume(self, session_id: str, request: AgentRequest) -> AgentResponse:
        return await self._invoke("resume", session_id, request)

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
