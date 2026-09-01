"""Native Slice 2 adapter composition for both bounded workflows."""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit

from .adapters import AgentAdapter, AgentRegistry
from .code_once import CodeOnceOrchestrator
from .council_once import CouncilOnceOrchestrator
from .grok_acp import GrokAdapter
from .native_adapters import ClaudeAdapter, CodexAdapter, ProbeProvider
from .redaction import KnownCredentials
from .schemas import AgentTarget, DialecticConfig
from .service import ExecutionContext

_CREDENTIAL_NAMES = {
    "codex": ("OPENAI_API_KEY",),
    "claude-code": ("ANTHROPIC_API_KEY",),
    "grok-build": ("XAI_API_KEY",),
}
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


class NativeCodeExecutor:
    def __init__(
        self,
        *,
        source_environment: Mapping[str, str] | None = None,
        probe_provider: ProbeProvider | None = None,
    ) -> None:
        self.source_environment = os.environ if source_environment is None else source_environment
        self.probe_provider = probe_provider

    async def __call__(self, context: ExecutionContext):  # type: ignore[no-untyped-def]
        driver, reviewers = AgentRegistry.code_targets(context.config)
        driver_adapter = self._adapter(
            driver,
            role="driver",
            access_mode="driver-write",
            context=context,
        )
        reviewer_adapters = {
            reviewer_id: self._adapter(
                target,
                role="reviewer",
                access_mode="packet-only",
                context=context,
            )
            for reviewer_id, target in reviewers
        }
        orchestrator = CodeOnceOrchestrator(
            driver_adapter=driver_adapter,
            reviewer_adapters=reviewer_adapters,
        )
        return await orchestrator(context)

    def _adapter(
        self,
        target: AgentTarget,
        *,
        role: str,
        access_mode: str,
        context: ExecutionContext,
    ) -> AgentAdapter:
        limits = context.config.limits
        common = {
            "role": role,
            "access_mode": access_mode,
            "store": context.service.store,
            "credentials": context.credentials,
            "preflight_seconds": limits.preflight_seconds,
            "capability_probe_seconds": limits.capability_probe_seconds,
            "stdout_limit": limits.max_agent_stdout_bytes,
            "stderr_limit": limits.max_agent_stderr_bytes,
            "graceful_kill_seconds": limits.graceful_kill_seconds,
            "source_environment": self.source_environment,
            "probe_provider": self.probe_provider,
            "research_mode": (
                context.config.research_mode
                if access_mode == "packet-only"
                else "offline"
            ),
        }
        adapter_type = {
            "codex": CodexAdapter,
            "claude-code": ClaudeAdapter,
            "grok-build": GrokAdapter,
        }[target.runtime]
        return adapter_type(target, **common)  # type: ignore[arg-type,return-value]


class NativeCouncilExecutor(NativeCodeExecutor):
    """Construct one isolated native adapter per Council execution context."""

    async def __call__(self, context: ExecutionContext):  # type: ignore[no-untyped-def]
        participants, moderator = AgentRegistry.council_targets(context.config)
        participant_adapters = {
            participant_id: self._adapter(
                target,
                role="participant",
                access_mode="packet-only",
                context=context,
            )
            for participant_id, target in participants
        }
        moderator_adapter = self._adapter(
            moderator,
            role="moderator",
            access_mode="packet-only",
            context=context,
        )
        return await CouncilOnceOrchestrator(
            participant_adapters=participant_adapters,
            moderator_adapter=moderator_adapter,
        )(context)


def native_credentials(
    config: DialecticConfig,
    mode: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> KnownCredentials:
    source = os.environ if environment is None else environment
    runtimes: set[str] = set()
    if mode == "code":
        driver, reviewers = AgentRegistry.code_targets(config)
        runtimes.add(driver.runtime)
        runtimes.update(target.runtime for _, target in reviewers)
    else:
        participants, moderator = AgentRegistry.council_targets(config)
        runtimes.update(target.runtime for _, target in participants)
        runtimes.add(moderator.runtime)
    names = {
        name
        for runtime in runtimes
        for name in _CREDENTIAL_NAMES[runtime]
    }
    for supplied_name, value in source.items():
        if supplied_name.upper() in _PROXY_NAMES and _proxy_contains_authentication(value):
            names.add(supplied_name)
    return KnownCredentials.from_environment(names, source)


def _proxy_contains_authentication(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.username is not None or parsed.password is not None
