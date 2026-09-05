"""Native Slice 2 adapter composition for both bounded workflows."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Callable, Literal, Mapping
from urllib.parse import urlsplit

from .adapters import AgentAdapter, AgentRegistry
from .code_once import CodeOnceOrchestrator
from .council_once import CouncilOnceOrchestrator
from .grok_acp import GrokAdapter
from .native_adapters import (
    ClaudeAdapter,
    CodexAdapter,
    NativePreflightMaterial,
    ProbeProvider,
)
from .redaction import KnownCredentials
from .schemas import (
    AgentTarget,
    DialecticConfig,
    DoctorReport,
    DoctorTargetReport,
)
from .service import DoctorContext, ExecutionContext
from .store import RunStore, canonical_json_bytes
from .turn_timing import MAXIMUM_TURN_SECONDS, TurnDeadlineController

_CREDENTIAL_NAMES = {
    "codex": ("OPENAI_API_KEY",),
    "claude-code": ("ANTHROPIC_API_KEY",),
    "grok-build": ("XAI_API_KEY",),
}
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

Role = Literal["driver", "reviewer", "participant", "moderator"]
AccessMode = Literal["driver-write", "packet-only"]
AdapterFactory = Callable[..., AgentAdapter]
IndexedDoctorTarget = tuple[int, Role, str, AgentTarget, AccessMode]


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
        role: Role,
        access_mode: AccessMode,
        context: ExecutionContext,
    ) -> AgentAdapter:
        return build_native_adapter(
            target,
            role=role,
            access_mode=access_mode,
            config=context.config,
            store=context.service.store,
            credentials=context.credentials,
            source_environment=self.source_environment,
            probe_provider=self.probe_provider,
            turn_deadlines=context.turn_deadlines,
        )


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


class NativeDoctor:
    """Run the same immutable native preflight used by real workflows."""

    def __init__(
        self,
        *,
        source_environment: Mapping[str, str] | None = None,
        probe_provider: ProbeProvider | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self.source_environment = (
            os.environ if source_environment is None else source_environment
        )
        self.probe_provider = probe_provider
        self.adapter_factory = adapter_factory or build_native_adapter

    async def __call__(self, context: DoctorContext) -> DoctorReport:
        targets = _doctor_targets(context.config, context.mode)

        async def inspect(
            index: int,
            role: Role,
            target_id: str,
            target: AgentTarget,
            access_mode: AccessMode,
        ) -> tuple[int, DoctorTargetReport]:
            try:
                adapter = self.adapter_factory(
                    target,
                    role=role,
                    access_mode=access_mode,
                    config=context.config,
                    store=context.store,
                    credentials=context.credentials,
                    source_environment=self.source_environment,
                    probe_provider=self.probe_provider,
                    turn_deadlines=None,
                )
                result = await adapter.preflight(target)
                material_reader = getattr(adapter, "preflight_material", None)
                if not callable(material_reader):
                    raise RuntimeError("native adapter did not expose preflight material")
                material = material_reader()
                if not isinstance(material, NativePreflightMaterial):
                    raise RuntimeError("native adapter returned invalid preflight material")
                attestation_sha = hashlib.sha256(
                    canonical_json_bytes(material.attestation)
                ).hexdigest()
                return (
                    index,
                    DoctorTargetReport(
                        role=role,
                        target_id=target_id,
                        target=target,
                        access_mode=access_mode,
                        ready=True,
                        resolved_requested_model=result.resolved_requested_model,
                        resolved_executable=str(material.resolved_executable),
                        cli_version=material.cli_version,
                        adapter_fixture_version=material.adapter_fixture_version,
                        prompt_transport=material.prompt_transport,
                        process_lifecycle=material.process_lifecycle,
                        capability_attestation_sha256=attestation_sha,
                        authentication_verified=result.authentication_verified,
                        diagnostic=None,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                diagnostic = context.credentials.redact_text(str(exc))
                return (
                    index,
                    DoctorTargetReport(
                        role=role,
                        target_id=target_id,
                        target=target,
                        access_mode=access_mode,
                        ready=False,
                        resolved_requested_model=None,
                        resolved_executable=None,
                        cli_version=None,
                        adapter_fixture_version=None,
                        prompt_transport=None,
                        process_lifecycle=None,
                        capability_attestation_sha256=None,
                        authentication_verified=False,
                        diagnostic=_bounded_diagnostic(diagnostic),
                    ),
                )

        indexed = [(index, *target) for index, target in enumerate(targets)]
        groups: dict[
            tuple[str, Role, AccessMode], list[IndexedDoctorTarget]
        ] = {}
        for item in indexed:
            _, role, _, target, access_mode = item
            groups.setdefault((target.runtime, role, access_mode), []).append(item)
        leaders = [items[0] for items in groups.values()]
        followers = [item for items in groups.values() for item in items[1:]]
        inspected = list(await asyncio.gather(*(inspect(*item) for item in leaders)))
        if followers:
            inspected.extend(
                await asyncio.gather(*(inspect(*item) for item in followers))
            )
        reports = [report for _, report in sorted(inspected)]
        return DoctorReport(
            tool_version=context.tool_version,
            mode=context.mode,
            config_sha256=context.config_sha256,
            state_root=str(context.store.state_root.resolve()),
            healthy=all(report.ready for report in reports),
            targets=reports,
        )


def build_native_adapter(
    target: AgentTarget,
    *,
    role: Role,
    access_mode: AccessMode,
    config: DialecticConfig,
    store: RunStore,
    credentials: KnownCredentials,
    source_environment: Mapping[str, str],
    probe_provider: ProbeProvider | None,
    turn_deadlines: TurnDeadlineController | None,
) -> AgentAdapter:
    limits = config.limits
    common = {
        "role": role,
        "access_mode": access_mode,
        "store": store,
        "credentials": credentials,
        "preflight_seconds": limits.preflight_seconds,
        "capability_probe_seconds": limits.capability_probe_seconds,
        "stdout_limit": limits.max_agent_stdout_bytes,
        "stderr_limit": limits.max_agent_stderr_bytes,
        "graceful_kill_seconds": limits.graceful_kill_seconds,
        "source_environment": source_environment,
        "probe_provider": probe_provider,
        "research_mode": config.research_mode if access_mode == "packet-only" else "offline",
        "turn_deadlines": turn_deadlines,
        "turn_max_seconds": MAXIMUM_TURN_SECONDS,
    }
    adapter_type = {
        "codex": CodexAdapter,
        "claude-code": ClaudeAdapter,
        "grok-build": GrokAdapter,
    }[target.runtime]
    return adapter_type(target, **common)  # type: ignore[arg-type,return-value]


def _doctor_targets(
    config: DialecticConfig, mode: str
) -> list[tuple[Role, str, AgentTarget, AccessMode]]:
    if mode == "code":
        driver, reviewers = AgentRegistry.code_targets(config)
        return [
            ("driver", "driver", driver, "driver-write"),
            *(
                ("reviewer", reviewer_id, target, "packet-only")
                for reviewer_id, target in reviewers
            ),
        ]
    participants, moderator = AgentRegistry.council_targets(config)
    return [
        *(
            ("participant", participant_id, target, "packet-only")
            for participant_id, target in participants
        ),
        ("moderator", "moderator", moderator, "packet-only"),
    ]


def _bounded_diagnostic(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")[:4_096]
    return encoded.decode("utf-8", errors="ignore") or "native preflight failed"


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
