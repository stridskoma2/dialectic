"""Bounded offline Council Once orchestration."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from .adapters import AgentAdapter, AgentProcessError, AgentRegistry, ModelMismatchError
from .capabilities import (
    BindingBarrier,
    CapabilityEvidenceError,
    build_capability_binding,
)
from .config import validate_model_bounds
from .contracts import (
    ARTIFACT_SCHEMA_VERSION,
    ResearchMode,
    TOOL_VERSION,
    ConsensusOutcome,
    TurnPhase,
)
from .native_adapters import (
    NativeEnvelopeError,
    NativeInvocationEvidence,
    NativePreflightError,
    NativeTurnError,
)
from .output import OutputError, extract_model_payload
from .research import persist_source_citations, research_policy
from .schemas import (
    AgentRequest,
    AgentRequestArtifact,
    AgentResponse,
    AgentTarget,
    AliasMapArtifact,
    CapabilityBindingArtifact,
    CandidateConclusion,
    CandidateConclusionArtifact,
    CouncilBallot,
    CouncilRevision,
    CouncilRevisionArtifact,
    DerivedBallot,
    ModeratorOpeningArtifact,
    OpeningPosition,
    OpeningPositionArtifact,
    RunRecord,
    TurnAttemptArtifact,
    derive_overall_vote,
)
from .service import DialecticFailure, ExecutionContext, WorkflowTimedOut
from .workflow_evidence import (
    GateAEvidence as _GateAEvidence,
    WorkflowEvidenceSupport,
    authorize_native_binding as _authorize_native_binding,
    bounded_preflight_diagnostic as _bounded_preflight_diagnostic,
    canonical_mapping_bytes as _canonical_dict_bytes,
    concrete_profile as _concrete_profile,
    empty_stream as _empty_stream,
    process_unit_id_for as _process_unit_id,
    redacted_response as _redacted_response,
    require_packet_bound,
    stage_preflight_requests,
    take_native_invocation_evidence as _take_native_invocation_evidence,
)


@dataclass(slots=True)
class _AttemptDraft:
    phase: TurnPhase
    operation: Literal["start", "resume"]
    request: AgentRequest
    request_sha256: str
    root: str
    response: AgentResponse | None = None
    persisted: bool = False


@dataclass(slots=True)
class _Participant:
    configured_id: str
    target_id: str
    alias: str
    target: AgentTarget
    adapter: AgentAdapter
    gate_a: _GateAEvidence
    binding_sha256: str
    binding_relative_path: str
    binding: CapabilityBindingArtifact
    neutral_directory: Path
    opening: OpeningPosition | None = None
    revision: CouncilRevision | None = None
    session_id: str | None = None
    draft: _AttemptDraft | None = None
    closed: bool = False
    lease_event_written: bool = False

    @property
    def persistent(self) -> bool:
        return self.gate_a.preflight.process_lifecycle == "persistent-acp-session"


class CouncilOnceOrchestrator:
    """Execute one blind opening, one cross-examination, moderation, and ballots."""

    def __init__(
        self,
        *,
        participant_adapters: Mapping[str, AgentAdapter],
        moderator_adapter: AgentAdapter,
    ) -> None:
        self.participant_adapters = dict(participant_adapters)
        self.moderator_adapter = moderator_adapter
        self._active_peer_failure: asyncio.Event | None = None
        self._workflow_timeout: asyncio.Event | None = None
        self._evidence = WorkflowEvidenceSupport()

    async def __call__(self, context: ExecutionContext) -> RunRecord:
        participants: list[_Participant] = []
        self._workflow_timeout = asyncio.Event()
        try:
            participants, moderator_target, moderator_gate = await self._preflight(context)
            return await self._run_with_deadline(
                context,
                participants,
                moderator_target,
                moderator_gate,
            )
        except WorkflowTimedOut:
            cleanup = await self._close_retained(
                context, participants, reason="workflow-timeout"
            )
            if cleanup is not None:
                raise cleanup
            raise
        except asyncio.CancelledError as exc:
            cleanup = await self._close_retained(
                context, participants, reason="cancelled"
            )
            if cleanup is not None:
                raise cleanup from exc
            raise
        except DialecticFailure as exc:
            cleanup = await self._close_retained(
                context, participants, reason="phase-failure"
            )
            if cleanup is not None:
                raise cleanup from exc
            raise
        except Exception as exc:
            cleanup = await self._close_retained(
                context, participants, reason="phase-failure"
            )
            if cleanup is not None:
                raise cleanup from exc
            raise

    async def _run_with_deadline(
        self,
        context: ExecutionContext,
        participants: list[_Participant],
        moderator_target: AgentTarget,
        moderator_gate: _GateAEvidence,
    ) -> RunRecord:
        workflow = asyncio.create_task(
            self._run(context, participants, moderator_target, moderator_gate)
        )
        deadline = asyncio.create_task(
            asyncio.sleep(context.config.limits.council_run_seconds)
        )
        try:
            done, _pending = await asyncio.wait(
                {workflow, deadline}, return_when=asyncio.FIRST_COMPLETED
            )
            if workflow in done:
                return await workflow
            assert self._workflow_timeout is not None
            self._workflow_timeout.set()
            workflow.cancel()
            result = (await asyncio.gather(workflow, return_exceptions=True))[0]
            if isinstance(result, DialecticFailure) and result.kind == "PROCESS_CLEANUP_FAILED":
                raise result
            raise WorkflowTimedOut("Council Once reached its overall wall-clock limit")
        except asyncio.CancelledError:
            workflow.cancel()
            await asyncio.gather(workflow, return_exceptions=True)
            raise
        finally:
            deadline.cancel()
            await asyncio.gather(deadline, return_exceptions=True)

    async def _preflight(
        self, context: ExecutionContext
    ) -> tuple[list[_Participant], AgentTarget, _GateAEvidence]:
        specs, moderator_target = AgentRegistry.council_targets(context.config)
        requests: list[
            tuple[str, str, str, AgentTarget, AgentAdapter]
        ] = []
        for index, (configured_id, target) in enumerate(specs):
            adapter = self.participant_adapters.get(configured_id)
            if adapter is None:
                raise DialecticFailure(
                    "PREFLIGHT_FAILED",
                    f"no adapter is configured for participant {configured_id}",
                )
            requests.append(
                (
                    "participant",
                    configured_id,
                    _target_id(index),
                    target,
                    adapter,
                )
            )
        requests.append(
            ("moderator", "moderator", "moderator", moderator_target, self.moderator_adapter)
        )

        async def one(
            role: str,
            configured_id: str,
            target_id: str,
            target: AgentTarget,
            adapter: AgentAdapter,
        ) -> tuple[tuple[str, str], _GateAEvidence]:
            try:
                result = await asyncio.wait_for(
                    adapter.preflight(target),
                    timeout=(
                        context.config.limits.preflight_seconds
                        + context.config.limits.capability_probe_seconds
                    ),
                )
            except Exception as exc:
                raise DialecticFailure(
                    "PREFLIGHT_FAILED",
                    f"target preflight failed for {configured_id}: "
                    f"{_bounded_preflight_diagnostic(exc)}",
                ) from exc
            if not result.authentication_verified or result.target != target:
                detail = (
                    "authentication was not verified"
                    if not result.authentication_verified
                    else "adapter returned a mismatched target"
                )
                raise DialecticFailure(
                    "PREFLIGHT_FAILED",
                    f"target preflight failed for {configured_id}: {detail}",
                )
            evidence = self._evidence.persist_gate_a(
                context,
                adapter=adapter,
                role=role,
                target_id=target_id,
                target=target,
                result=result,
                access_mode="packet-only",
            )
            return (role, configured_id), evidence

        leaders, followers = stage_preflight_requests(
            requests,
            cohort_key=lambda request: request[3].runtime,
        )
        pairs = list(await asyncio.gather(*(one(*request) for request in leaders)))
        if followers:
            pairs.extend(await asyncio.gather(*(one(*request) for request in followers)))
        gates = dict(pairs)
        aliases = {
            _alias(index): target for index, (_configured_id, target) in enumerate(specs)
        }
        context.service.store.write_artifact(
            context.handle,
            "council/aliases.json",
            AliasMapArtifact(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                aliases=aliases,
            ),
        )

        barrier = BindingBarrier(_target_id(index) for index in range(len(specs)))
        participants: list[_Participant] = []
        for index, (configured_id, target) in enumerate(specs):
            target_id = _target_id(index)
            alias = _alias(index)
            adapter = self.participant_adapters[configured_id]
            neutral = context.service.store.create_role_directory(
                context.handle, "council-role-directories", target_id
            )
            gate = gates[("participant", configured_id)]
            binding, binding_sha, relative = self._bind_packet_role(
                context,
                role="participant",
                target_id=target_id,
                phase="opening",
                neutral=neutral,
                gate=gate,
                adapter=adapter,
            )
            barrier.add(target_id, binding)
            participants.append(
                _Participant(
                    configured_id=configured_id,
                    target_id=target_id,
                    alias=alias,
                    target=target,
                    adapter=adapter,
                    gate_a=gate,
                    binding_sha256=binding_sha,
                    binding_relative_path=relative,
                    binding=binding,
                    neutral_directory=neutral,
                )
            )
        barrier.authorize_launch()
        return participants, moderator_target, gates[("moderator", "moderator")]

    async def _run(
        self,
        context: ExecutionContext,
        participants: list[_Participant],
        moderator_target: AgentTarget,
        moderator_gate: _GateAEvidence,
    ) -> RunRecord:
        council = context.config.council
        if council is None:
            raise DialecticFailure("INTERNAL_ERROR", "validated council configuration vanished")
        opening_prompt = _opening_prompt(
            context.input_text, research_mode=context.config.research_mode
        )
        moderator_opening_prompt = (
            _moderator_opening_prompt(
                context.input_text, research_mode=context.config.research_mode
            )
            if council.moderator_mode == "independent-opening"
            else None
        )
        opening_packets = [(item.target_id, opening_prompt) for item in participants]
        if moderator_opening_prompt is not None:
            opening_packets.append(("moderator", moderator_opening_prompt))
        self._require_all_packets(context, opening_packets, OpeningPosition)
        context.service.advance_phase(context.handle, "OPENING_POSITIONS")
        context.service.mark_model_work_started(context.handle)

        moderator_opening: OpeningPosition | None = None
        if moderator_opening_prompt is not None:
            moderator_opening_directory = context.service.store.create_role_directory(
                context.handle, "council-role-directories", "moderator-opening"
            )
            opening_binding, opening_binding_sha, opening_relative = self._bind_packet_role(
                context,
                role="moderator",
                target_id="moderator",
                phase="opening",
                neutral=moderator_opening_directory,
                gate=moderator_gate,
                adapter=self.moderator_adapter,
            )
            self._evidence.validate_launch_evidence(
                context,
                gate_a=moderator_gate,
                binding=opening_binding,
                binding_sha256=opening_binding_sha,
                binding_relative_path=opening_relative,
                dynamic_paths={"neutral_role_dir": moderator_opening_directory},
            )
            moderator_opening_turn = await self._evidence.invoke_turn(
                context,
                adapter=self.moderator_adapter,
                target=moderator_target,
                gate_a=moderator_gate,
                binding_sha256=opening_binding_sha,
                role="moderator",
                target_id="moderator",
                phase="opening",
                operation="start",
                prompt=moderator_opening_prompt,
                output_schema=OpeningPosition.model_json_schema(),
                working_directory=moderator_opening_directory,
                access_mode="packet-only",
                failure_kind="MODERATOR_FAILED",
                workflow_timeout=self._workflow_timeout,
            )
            try:
                moderator_opening = extract_model_payload(
                    moderator_opening_turn.response.text,
                    OpeningPosition,
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                )
            except OutputError as exc:
                raise DialecticFailure(
                    "MODERATOR_FAILED", "moderator returned an invalid independent opening"
                ) from exc
            context.service.store.write_artifact(
                context.handle,
                "council/moderator-opening.json",
                ModeratorOpeningArtifact(
                    artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                    tool_version=TOOL_VERSION,
                    moderator_target=moderator_target,
                    packet_sha256=hashlib.sha256(
                        moderator_opening_prompt.encode("utf-8")
                    ).hexdigest(),
                    position=moderator_opening,
                ),
            )

        async def opening(item: _Participant) -> None:
            self._revalidate(context, item)
            if item.persistent:
                draft = self._make_draft(
                    context, item, "opening", "start", opening_prompt, OpeningPosition
                )
                response = await self._persistent_call(context, item, draft)
            else:
                turn = await self._evidence.invoke_turn(
                    context,
                    adapter=item.adapter,
                    target=item.target,
                    gate_a=item.gate_a,
                    binding_sha256=item.binding_sha256,
                    role="participant",
                    target_id=item.target_id,
                    phase="opening",
                    operation="start",
                    prompt=opening_prompt,
                    output_schema=OpeningPosition.model_json_schema(),
                    working_directory=item.neutral_directory,
                    access_mode="packet-only",
                    failure_kind="NO_QUORUM",
                    peer_failure=self._active_peer_failure,
                    workflow_timeout=self._workflow_timeout,
                )
                response = turn.response
            if response.session_id is None:
                if item.persistent and item.draft is not None:
                    evidence = _take_native_invocation_evidence(item.adapter)
                    if evidence is not None:
                        self._persist_attempt(context, item, item.draft, evidence)
                        item.closed = evidence.process_disposition in {
                            "closed", "cleanup-failed"
                        }
                raise DialecticFailure(
                    "NO_QUORUM", f"{item.alias} returned no resumable session id"
                )
            item.session_id = response.session_id
            try:
                item.opening = extract_model_payload(
                    response.text,
                    OpeningPosition,
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                )
            except OutputError as exc:
                raise DialecticFailure(
                    "NO_QUORUM", f"invalid opening position from {item.alias}"
                ) from exc
            artifact = OpeningPositionArtifact(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                participant_alias=item.alias,
                packet_sha256=hashlib.sha256(opening_prompt.encode("utf-8")).hexdigest(),
                position=item.opening,
            )
            context.service.store.write_artifact(
                context.handle, f"council/opening/{item.target_id}.json", artifact
            )

        await self._participant_cohort(participants, opening)
        ledger = _position_ledger(participants, moderator_opening)

        cross_prompts = {
            item.target_id: _cross_prompt(
                context.input_text,
                ledger,
                item.alias,
                research_mode=context.config.research_mode,
            )
            for item in participants
        }
        self._require_all_packets(
            context,
            [(item.target_id, cross_prompts[item.target_id]) for item in participants],
            CouncilRevision,
        )
        for item in participants:
            if item.persistent:
                draft = self._make_draft(
                    context,
                    item,
                    "cross-examination",
                    "resume",
                    cross_prompts[item.target_id],
                    CouncilRevision,
                )
                await self._prepare_persistent(context, item, draft)
        context.service.advance_phase(context.handle, "CROSS_EXAMINATION")

        async def cross(item: _Participant) -> None:
            self._revalidate(context, item)
            if item.persistent:
                assert item.draft is not None
                response = await self._persistent_call(context, item, item.draft)
            else:
                turn = await self._evidence.invoke_turn(
                    context,
                    adapter=item.adapter,
                    target=item.target,
                    gate_a=item.gate_a,
                    binding_sha256=item.binding_sha256,
                    role="participant",
                    target_id=item.target_id,
                    phase="cross-examination",
                    operation="resume",
                    session_id=item.session_id,
                    prompt=cross_prompts[item.target_id],
                    output_schema=CouncilRevision.model_json_schema(),
                    working_directory=item.neutral_directory,
                    access_mode="packet-only",
                    failure_kind="NO_QUORUM",
                    peer_failure=self._active_peer_failure,
                    workflow_timeout=self._workflow_timeout,
                )
                response = turn.response
            if response.session_id != item.session_id:
                raise DialecticFailure(
                    "NO_QUORUM", f"{item.alias} changed its resumable session id"
                )
            try:
                item.revision = extract_model_payload(
                    response.text,
                    CouncilRevision,
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                )
            except OutputError as exc:
                raise DialecticFailure(
                    "NO_QUORUM", f"invalid cross-examination from {item.alias}"
                ) from exc
            artifact = CouncilRevisionArtifact(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                participant_alias=item.alias,
                packet_sha256=hashlib.sha256(
                    cross_prompts[item.target_id].encode("utf-8")
                ).hexdigest(),
                revision=item.revision,
            )
            context.service.store.write_artifact(
                context.handle,
                f"council/cross-examination/{item.target_id}.json",
                artifact,
            )

        await self._participant_cohort(participants, cross)

        moderation_prompt = _moderator_prompt(
            context.input_text,
            ledger,
            _revision_ledger(participants),
            participant_aliases=[item.alias for item in participants],
            research_mode=context.config.research_mode,
        )
        self._require_all_packets(context, [("moderator", moderation_prompt)], CandidateConclusion)
        moderator_directory = context.service.store.create_role_directory(
            context.handle, "council-role-directories", "moderator"
        )
        moderator_binding, moderator_binding_sha, moderator_relative = self._bind_packet_role(
            context,
            role="moderator",
            target_id="moderator",
            phase="candidate",
            neutral=moderator_directory,
            gate=moderator_gate,
            adapter=self.moderator_adapter,
        )
        self._evidence.validate_launch_evidence(
            context,
            gate_a=moderator_gate,
            binding=moderator_binding,
            binding_sha256=moderator_binding_sha,
            binding_relative_path=moderator_relative,
            dynamic_paths={"neutral_role_dir": moderator_directory},
        )
        context.service.advance_phase(context.handle, "MODERATION")
        moderator_turn = await self._evidence.invoke_turn(
            context,
            adapter=self.moderator_adapter,
            target=moderator_target,
            gate_a=moderator_gate,
            binding_sha256=moderator_binding_sha,
            role="moderator",
            target_id="moderator",
            phase="candidate",
            operation="start",
            prompt=moderation_prompt,
            output_schema=CandidateConclusion.model_json_schema(),
            working_directory=moderator_directory,
            access_mode="packet-only",
            failure_kind="MODERATOR_FAILED",
            workflow_timeout=self._workflow_timeout,
        )
        try:
            candidate = extract_model_payload(
                moderator_turn.response.text,
                CandidateConclusion,
                max_chars=context.config.limits.max_model_field_chars,
                max_items=context.config.limits.max_model_list_items,
                context={
                    "max_propositions": context.config.limits.max_propositions,
                    "participant_aliases": [item.alias for item in participants],
                },
            )
        except OutputError as exc:
            raise DialecticFailure("MODERATOR_FAILED", "moderator returned an invalid candidate") from exc
        context.service.store.write_artifact(
            context.handle,
            "council/candidate.json",
            CandidateConclusionArtifact(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                moderator_target=moderator_target,
                packet_sha256=hashlib.sha256(moderation_prompt.encode("utf-8")).hexdigest(),
                candidate=candidate,
            ),
        )

        ballot_prompts = {
            item.target_id: _ballot_prompt(
                candidate,
                item.alias,
                research_mode=context.config.research_mode,
            )
            for item in participants
        }
        self._require_all_packets(
            context,
            [(item.target_id, ballot_prompts[item.target_id]) for item in participants],
            CouncilBallot,
        )
        for item in participants:
            if item.persistent:
                draft = self._make_draft(
                    context,
                    item,
                    "ballot",
                    "resume",
                    ballot_prompts[item.target_id],
                    CouncilBallot,
                )
                await self._prepare_persistent(context, item, draft)
        context.service.advance_phase(context.handle, "BALLOTS")
        proposition_ids = [proposition.id for proposition in candidate.propositions]
        derived: list[DerivedBallot] = []

        async def ballot(item: _Participant) -> None:
            self._revalidate(context, item)
            if item.persistent:
                assert item.draft is not None
                response = await self._persistent_call(context, item, item.draft)
            else:
                turn = await self._evidence.invoke_turn(
                    context,
                    adapter=item.adapter,
                    target=item.target,
                    gate_a=item.gate_a,
                    binding_sha256=item.binding_sha256,
                    role="participant",
                    target_id=item.target_id,
                    phase="ballot",
                    operation="resume",
                    session_id=item.session_id,
                    prompt=ballot_prompts[item.target_id],
                    output_schema=CouncilBallot.model_json_schema(),
                    working_directory=item.neutral_directory,
                    access_mode="packet-only",
                    failure_kind="NO_QUORUM",
                    peer_failure=self._active_peer_failure,
                    workflow_timeout=self._workflow_timeout,
                )
                response = turn.response
            if response.session_id != item.session_id:
                raise DialecticFailure("NO_QUORUM", f"{item.alias} changed session during ballot")
            try:
                raw_ballot = extract_model_payload(
                    response.text,
                    CouncilBallot,
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                    context={"proposition_ids": proposition_ids},
                )
            except OutputError as exc:
                raise DialecticFailure("NO_QUORUM", f"invalid ballot from {item.alias}") from exc
            artifact = DerivedBallot(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                participant_alias=item.alias,
                ballot=raw_ballot,
                derived_overall_vote=derive_overall_vote(raw_ballot),
            )
            if item.persistent:
                cleanup = await self._close_one(context, item, reason="completed")
                if cleanup is not None:
                    raise cleanup
            context.service.store.write_artifact(
                context.handle, f"council/ballots/{item.target_id}.json", artifact
            )
            derived.append(artifact)

        await self._participant_cohort(participants, ballot)
        if any(item.persistent and not item.closed for item in participants):
            raise DialecticFailure("INTERNAL_ERROR", "a retained council lease survived ballots")

        by_alias = {ballot.participant_alias: ballot for ballot in derived}
        derived = [by_alias[item.alias] for item in participants]
        outcome = _consensus_outcome(
            derived,
            participant_count=len(participants),
            max_dissenters=council.consensus.max_dissenters,
        )
        context.service.advance_phase(context.handle, "REPORTING")
        notes = _report_lines(
            candidate,
            derived,
            participants,
            moderator_opening=moderator_opening,
        )
        artifact_paths = {
            "aliases": "council/aliases.json",
            "candidate": "council/candidate.json",
            "ballots": "council/ballots",
            "openings": "council/opening",
            "revisions": "council/cross-examination",
        }
        if moderator_opening is not None:
            artifact_paths["moderator_opening"] = "council/moderator-opening.json"
        if context.config.research_mode == "live-web":
            artifact_paths["research_sources"] = "research/sources"
        return context.service.finalize_council(
            context.handle,
            outcome,
            unresolved_items=candidate.unresolved_questions,
            artifact_paths=artifact_paths,
            markdown_notes=notes,
        )

    def _bind_packet_role(
        self,
        context: ExecutionContext,
        *,
        role: str,
        target_id: str,
        phase: str,
        neutral: Path,
        gate: _GateAEvidence,
        adapter: AgentAdapter,
    ) -> tuple[CapabilityBindingArtifact, str, str]:
        dynamic_paths = {"neutral_role_dir": neutral}
        concrete = _concrete_profile(gate.fixture, dynamic_paths)
        try:
            binding = build_capability_binding(
                binding_id=f"{context.handle.run_id}:{target_id}:{phase}",
                role=role,
                target_id=target_id,
                access_mode="packet-only",
                target_preflight_bytes=gate.preflight_bytes,
                attestation_bytes=gate.attestation_bytes,
                attestation=gate.attestation,
                fixture=gate.fixture,
                dynamic_paths=dynamic_paths,
                supplied_concrete_profile=concrete,
            )
        except CapabilityEvidenceError as exc:
            raise DialecticFailure("PREFLIGHT_FAILED", f"{role} capability binding failed") from exc
        relative = f"audit/capabilities/{role}/{target_id}/{phase}.binding.json"
        sha = context.service.store.write_artifact(context.handle, relative, binding)
        _authorize_native_binding(adapter, binding, concrete, dynamic_paths)
        return binding, sha, relative

    def _revalidate(self, context: ExecutionContext, item: _Participant) -> None:
        self._evidence.validate_launch_evidence(
            context,
            gate_a=item.gate_a,
            binding=item.binding,
            binding_sha256=item.binding_sha256,
            binding_relative_path=item.binding_relative_path,
            dynamic_paths={"neutral_role_dir": item.neutral_directory},
        )

    def _make_draft(
        self,
        context: ExecutionContext,
        item: _Participant,
        phase: TurnPhase,
        operation: Literal["start", "resume"],
        prompt: str,
        schema: type[Any],
    ) -> _AttemptDraft:
        persisted_prompt = context.credentials.redact_text(prompt)
        artifact = AgentRequestArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            role="participant",
            target_id=item.target_id,
            turn_phase=phase,
            outbound_prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            persisted_prompt_sha256=hashlib.sha256(persisted_prompt.encode("utf-8")).hexdigest(),
            prompt=persisted_prompt,
            output_schema=schema.model_json_schema(),
            timeout_seconds=context.config.limits.agent_turn_seconds,
            access_mode="packet-only",
        )
        root = f"turns/participant/{item.target_id}/{phase}"
        request_sha = context.service.store.write_artifact(
            context.handle, f"{root}.request.json", artifact
        )
        return _AttemptDraft(
            phase=phase,
            operation=operation,
            request=AgentRequest(
                role="participant",
                target_id=item.target_id,
                turn_phase=phase,
                prompt=prompt,
                output_schema=schema.model_json_schema(),
                timeout_seconds=context.config.limits.agent_turn_seconds,
                working_directory=str(item.neutral_directory),
                access_mode="packet-only",
            ),
            request_sha256=request_sha,
            root=root,
        )

    async def _persistent_call(
        self, context: ExecutionContext, item: _Participant, draft: _AttemptDraft
    ) -> AgentResponse:
        item.draft = draft
        try:
            invocation = (
                item.adapter.start(draft.request)
                if draft.operation == "start"
                else item.adapter.resume(item.session_id or "", draft.request)
            )
            raw = await context.turn_deadlines.wait_for(
                draft.request, item.target.runtime, invocation
            )
            response = AgentResponse.model_validate(raw)
            validate_model_bounds(
                response.model_dump(mode="python"),
                max_chars=context.config.limits.max_model_field_chars,
                max_items=context.config.limits.max_model_list_items,
            )
            if response.runtime != item.target.runtime or response.requested_model != item.target.model:
                raise DialecticFailure("NO_QUORUM", "participant response target changed")
            draft.response = _redacted_response(response, context)
            return draft.response
        except BaseException as exc:
            evidence = _take_native_invocation_evidence(item.adapter)
            if evidence is not None:
                if isinstance(exc, asyncio.CancelledError):
                    if self._workflow_timeout is not None and self._workflow_timeout.is_set():
                        evidence = replace(
                            evidence,
                            attempt_end_reason="timeout",
                            failure_kind=None,
                        )
                    elif (
                        self._active_peer_failure is not None
                        and self._active_peer_failure.is_set()
                    ):
                        evidence = replace(evidence, attempt_end_reason="peer-failure")
                self._persist_attempt(context, item, draft, evidence)
                if evidence.process_disposition in {"closed", "cleanup-failed"}:
                    item.closed = True
            elif item.session_id is None:
                self._persist_unstarted(context, item, draft, exc)
            if isinstance(exc, asyncio.CancelledError):
                if evidence is not None and evidence.failure_kind == "PROCESS_CLEANUP_FAILED":
                    raise DialecticFailure(
                        "PROCESS_CLEANUP_FAILED", "participant cleanup failed during cancellation"
                    ) from exc
                raise
            if isinstance(exc, DialecticFailure):
                raise
            if isinstance(exc, ModelMismatchError):
                raise DialecticFailure("MODEL_MISMATCH", str(exc)) from exc
            if isinstance(exc, NativeTurnError) and exc.kind is not None:
                raise DialecticFailure(exc.kind, exc.detail) from exc
            if isinstance(exc, NativePreflightError):
                raise DialecticFailure(
                    "NO_QUORUM",
                    "native turn preparation failed: "
                    f"{_bounded_preflight_diagnostic(exc)}",
                ) from exc
            if isinstance(exc, (NativeEnvelopeError, AgentProcessError, TimeoutError)):
                raise DialecticFailure("NO_QUORUM", "persistent participant turn failed") from exc
            raise DialecticFailure(
                "NO_QUORUM", f"participant invocation failed: {type(exc).__name__}"
            ) from exc

    async def _prepare_persistent(
        self, context: ExecutionContext, item: _Participant, next_draft: _AttemptDraft
    ) -> None:
        if item.session_id is None or item.draft is None:
            raise DialecticFailure("NO_QUORUM", f"{item.alias} lacks retained lease evidence")
        self._revalidate(context, item)
        prepare = getattr(item.adapter, "prepare_resume", None)
        if not callable(prepare):
            raise DialecticFailure("NO_QUORUM", f"{item.alias} cannot resume its retained lease")
        try:
            await prepare(item.session_id, next_draft.request)
            evidence = _take_native_invocation_evidence(item.adapter)
            if evidence is None:
                raise DialecticFailure("NO_QUORUM", "retained epoch produced no evidence")
            self._persist_attempt(context, item, item.draft, evidence)
            if not item.lease_event_written:
                self._append_lease_event(context, item, "session_lease_acquired", evidence)
                item.lease_event_written = True
            self._append_lease_event(context, item, "capture_epoch_closed", evidence)
            item.draft = next_draft
        except DialecticFailure:
            raise
        except NativeTurnError as exc:
            evidence = _take_native_invocation_evidence(item.adapter)
            if evidence is not None and item.draft is not None:
                self._persist_attempt(context, item, item.draft, evidence)
                item.closed = evidence.process_disposition in {"closed", "cleanup-failed"}
            raise DialecticFailure(exc.kind or "NO_QUORUM", exc.detail) from exc
        except Exception as exc:
            evidence = _take_native_invocation_evidence(item.adapter)
            if evidence is not None and item.draft is not None:
                self._persist_attempt(context, item, item.draft, evidence)
                item.closed = evidence.process_disposition in {"closed", "cleanup-failed"}
            raise DialecticFailure("NO_QUORUM", "retained epoch transition failed") from exc

    async def _close_one(
        self, context: ExecutionContext, item: _Participant, *, reason: str
    ) -> DialecticFailure | None:
        if not item.persistent or item.closed or item.session_id is None:
            return None
        close = getattr(item.adapter, "close_retained_session", None)
        if not callable(close):
            return DialecticFailure("PROCESS_CLEANUP_FAILED", "retained adapter lacks cleanup")
        error: BaseException | None = None
        try:
            await close(item.session_id, reason)
        except BaseException as exc:
            error = exc
        evidence = _take_native_invocation_evidence(item.adapter)
        if evidence is None or item.draft is None:
            return DialecticFailure(
                "PROCESS_CLEANUP_FAILED", f"cleanup evidence is absent for {item.alias}"
            )
        if not item.draft.persisted:
            self._persist_attempt(context, item, item.draft, evidence)
            if not item.lease_event_written:
                self._append_lease_event(context, item, "session_lease_acquired", evidence)
                item.lease_event_written = True
            self._append_lease_event(context, item, "capture_epoch_closed", evidence)
        item.closed = evidence.process_disposition in {"closed", "cleanup-failed"}
        self._append_lease_event(context, item, "session_lease_closed", evidence)
        if not item.closed or evidence.process_disposition == "cleanup-failed":
            return DialecticFailure(
                "PROCESS_CLEANUP_FAILED", f"retained cleanup failed for {item.alias}"
            )
        if evidence.failure_kind == "AGENT_OUTPUT_TOO_LARGE":
            return DialecticFailure(
                "AGENT_OUTPUT_TOO_LARGE", f"retained output exceeded bounds for {item.alias}"
            )
        if error is not None:
            if isinstance(error, NativeTurnError) and error.kind is not None:
                return DialecticFailure(error.kind, error.detail)
            return DialecticFailure(
                "PROCESS_CLEANUP_FAILED", f"retained cleanup raised {type(error).__name__}"
            )
        return None

    async def _close_retained(
        self, context: ExecutionContext, participants: Sequence[_Participant], *, reason: str
    ) -> DialecticFailure | None:
        failures = await asyncio.gather(
            *(self._close_one(context, item, reason=reason) for item in participants),
            return_exceptions=True,
        )
        normalized = [failure for failure in failures if isinstance(failure, DialecticFailure)]
        cleanup = next(
            (failure for failure in normalized if failure.kind == "PROCESS_CLEANUP_FAILED"),
            None,
        )
        return cleanup or (normalized[0] if normalized else None)

    def _persist_attempt(
        self,
        context: ExecutionContext,
        item: _Participant,
        draft: _AttemptDraft,
        evidence: NativeInvocationEvidence,
    ) -> None:
        if draft.persisted:
            raise DialecticFailure("INTERNAL_ERROR", "attempt evidence was persisted twice")
        context.service.store.write_artifact(
            context.handle, f"{draft.root}.stdout.txt", evidence.stdout.persisted
        )
        context.service.store.write_artifact(
            context.handle, f"{draft.root}.stderr.txt", evidence.stderr.persisted
        )
        failure_kind = evidence.failure_kind
        attempt = TurnAttemptArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            role="participant",
            target_id=item.target_id,
            turn_phase=draft.phase,
            operation=draft.operation,
            request_artifact_sha256=draft.request_sha256,
            target_preflight_artifact_sha256=item.gate_a.preflight_sha256,
            capability_binding_artifact_sha256=item.binding_sha256,
            started_at=evidence.started_at,
            response_completed_at=(
                evidence.response_completed_at if draft.response is not None else None
            ),
            capture_completed_at=evidence.capture_completed_at,
            process_origin=evidence.process_origin,
            process_lifecycle=evidence.process_lifecycle,
            process_unit_id=evidence.process_unit_id,
            process_exit_code=evidence.process_exit_code,
            attempt_end_reason=evidence.attempt_end_reason,
            failure_kind=failure_kind,
            process_disposition=evidence.process_disposition,
            stdout=evidence.stdout.result,
            stderr=evidence.stderr.result,
            response=draft.response,
            bounded_diagnostic=evidence.bounded_diagnostic,
        )
        context.service.store.write_artifact(
            context.handle, f"{draft.root}.attempt.json", attempt
        )
        if draft.response is not None and context.config.research_mode == "live-web":
            persist_source_citations(
                context,
                role="participant",
                target_id=item.target_id,
                phase=draft.phase,
                response_text=draft.response.text,
                captured_at=evidence.capture_completed_at,
            )
        draft.persisted = True

    def _persist_unstarted(
        self,
        context: ExecutionContext,
        item: _Participant,
        draft: _AttemptDraft,
        error: BaseException,
    ) -> None:
        now = datetime.now(UTC)
        limit_out = context.config.limits.max_agent_stdout_bytes
        limit_err = context.config.limits.max_agent_stderr_bytes
        context.service.store.write_artifact(context.handle, f"{draft.root}.stdout.txt", b"")
        context.service.store.write_artifact(context.handle, f"{draft.root}.stderr.txt", b"")
        attempt = TurnAttemptArtifact(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            role="participant",
            target_id=item.target_id,
            turn_phase=draft.phase,
            operation=draft.operation,
            request_artifact_sha256=draft.request_sha256,
            target_preflight_artifact_sha256=item.gate_a.preflight_sha256,
            capability_binding_artifact_sha256=item.binding_sha256,
            started_at=now,
            response_completed_at=None,
            capture_completed_at=now,
            process_origin="none",
            process_lifecycle="persistent-acp-session",
            process_unit_id=None,
            process_exit_code=None,
            attempt_end_reason=(
                "cancelled" if isinstance(error, asyncio.CancelledError) else "launch-failed"
            ),
            failure_kind="NO_QUORUM",
            process_disposition="not-started",
            stdout=_empty_stream(limit_out),
            stderr=_empty_stream(limit_err),
            response=None,
            bounded_diagnostic=f"participant invocation failed: {type(error).__name__}",
        )
        context.service.store.write_artifact(
            context.handle, f"{draft.root}.attempt.json", attempt
        )
        draft.persisted = True

    async def _participant_cohort(
        self,
        participants: Sequence[_Participant],
        operation: Callable[[_Participant], Awaitable[None]],
    ) -> None:
        ready = 0
        ready_lock = asyncio.Lock()
        launch = asyncio.Event()

        async def admitted(item: _Participant) -> None:
            nonlocal ready
            async with ready_lock:
                ready += 1
                if ready == len(participants):
                    launch.set()
            await launch.wait()
            await operation(item)

        peer_failure = asyncio.Event()
        self._active_peer_failure = peer_failure
        tasks = [asyncio.create_task(admitted(item)) for item in participants]
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_EXCEPTION
                )
                failures: list[BaseException] = []
                for task in done:
                    if task.cancelled():
                        failures.append(asyncio.CancelledError())
                    else:
                        failure = task.exception()
                        if failure is not None:
                            failures.append(failure)
                if failures:
                    peer_failure.set()
                    for task in pending:
                        task.cancel()
                    results = await asyncio.gather(*pending, return_exceptions=True)
                    failures.extend(
                        value for value in results if isinstance(value, BaseException)
                    )
                    cleanup = next(
                        (
                            value
                            for value in failures
                            if isinstance(value, DialecticFailure)
                            and value.kind == "PROCESS_CLEANUP_FAILED"
                        ),
                        None,
                    )
                    if cleanup is not None:
                        raise cleanup
                    primary = failures[0]
                    if isinstance(primary, DialecticFailure):
                        raise primary
                    if isinstance(primary, asyncio.CancelledError):
                        raise primary
                    raise DialecticFailure("NO_QUORUM", "participant cohort failed") from primary
        except asyncio.CancelledError:
            if self._workflow_timeout is None or not self._workflow_timeout.is_set():
                peer_failure.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._active_peer_failure = None

    @staticmethod
    def _require_all_packets(
        context: ExecutionContext,
        packets: Sequence[tuple[str, str]],
        schema: type[Any],
    ) -> None:
        output_schema = schema.model_json_schema()
        for target_id, prompt in packets:
            require_packet_bound(
                context,
                prompt,
                output_schema=output_schema,
                target_id=target_id,
            )

    @staticmethod
    def _append_lease_event(
        context: ExecutionContext,
        item: _Participant,
        event_type: str,
        evidence: NativeInvocationEvidence | None,
    ) -> None:
        record = context.service.store.read_handle(context.handle)
        context.service.store.append_event(
            context.handle,
            phase=record.phase,
            event_type=event_type,
            payload={
                "role": "participant",
                "alias": item.target_id,
                "turn_phase": item.draft.phase if item.draft is not None else "opening",
                "process_unit_id": (
                    evidence.process_unit_id
                    if evidence is not None
                    else _process_unit_id(
                        context.handle.run_id, "participant", item.target_id, "opening"
                    )
                ),
                "disposition": (
                    evidence.process_disposition
                    if evidence is not None
                    else "retained-for-session"
                ),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )


def _target_id(index: int) -> str:
    return f"participant-{chr(ord('a') + index)}"


def _alias(index: int) -> str:
    return f"Participant {chr(ord('A') + index)}"


def _opening_prompt(
    prompt: str, *, research_mode: ResearchMode = "offline"
) -> str:
    return _research_packet(
        {
            "phase": "opening",
            "prompt": prompt,
            "instruction": "Give one independent position without assuming or referencing any peer.",
            "output_schema": OpeningPosition.model_json_schema(),
        },
        research_mode,
    )


def _moderator_opening_prompt(
    prompt: str, *, research_mode: ResearchMode = "offline"
) -> str:
    return _research_packet(
        {
            "phase": "opening",
            "prompt": prompt,
            "instruction": (
                "Give one independent answer before seeing any participant response. "
                "You are non-voting, and a later fresh moderator session will synthesize "
                "the council result."
            ),
            "output_schema": OpeningPosition.model_json_schema(),
        },
        research_mode,
    )


def _position_ledger(
    participants: Sequence[_Participant],
    moderator_opening: OpeningPosition | None = None,
) -> dict[str, Any]:
    positions: list[dict[str, Any]] = []
    if moderator_opening is not None:
        positions.append(
            {
                "alias": "Moderator opening",
                "position": moderator_opening.model_dump(mode="json"),
            }
        )
    positions.extend(
        {"alias": item.alias, "position": item.opening.model_dump(mode="json")}
        for item in participants
        if item.opening is not None
    )
    return {
        "positions": positions
    }


def _revision_ledger(participants: Sequence[_Participant]) -> dict[str, Any]:
    return {
        "revisions": [
            {"alias": item.alias, "revision": item.revision.model_dump(mode="json")}
            for item in participants
            if item.revision is not None
        ]
    }


def _cross_prompt(
    prompt: str,
    ledger: Mapping[str, Any],
    alias: str,
    *,
    research_mode: ResearchMode = "offline",
) -> str:
    return _research_packet(
        {
            "phase": "cross-examination",
            "prompt": prompt,
            "self_alias": alias,
            "position_ledger": ledger,
            "instruction": (
                "Identify the strongest opposing argument and unsupported assumptions, "
                "state what changed your view, and submit one revised conclusion."
            ),
            "output_schema": CouncilRevision.model_json_schema(),
        },
        research_mode,
    )


def _moderator_prompt(
    prompt: str,
    positions: Mapping[str, Any],
    revisions: Mapping[str, Any],
    *,
    participant_aliases: Sequence[str],
    research_mode: ResearchMode = "offline",
) -> str:
    return _research_packet(
        {
            "phase": "candidate",
            "prompt": prompt,
            "position_ledger": positions,
            "revision_ledger": revisions,
            "eligible_supporting_participants": list(participant_aliases),
            "instruction": (
                "Act as a fresh non-voting moderator. Produce a concise candidate answer "
                "split into independently ratifiable propositions. Proposition IDs must "
                "match [a-z][a-z0-9-]{0,31} (for example p-1), and supporting_participants "
                "may contain only aliases listed in eligible_supporting_participants. "
                "If a Moderator opening appears in position_ledger, treat it as a blind "
                "non-voting position rather than an eligible supporting participant."
            ),
            "output_schema": CandidateConclusion.model_json_schema(),
        },
        research_mode,
    )


def _ballot_prompt(
    candidate: CandidateConclusion,
    alias: str,
    *,
    research_mode: ResearchMode = "offline",
) -> str:
    return _research_packet(
        {
            "phase": "ballot",
            "self_alias": alias,
            "candidate": candidate.model_dump(mode="json"),
            "instruction": "Vote exactly once on every proposition. Do not submit an overall vote.",
            "output_schema": CouncilBallot.model_json_schema(),
        },
        research_mode,
    )


def _research_packet(payload: dict[str, Any], research_mode: ResearchMode) -> str:
    if research_mode == "live-web":
        payload["research_policy"] = research_policy()
    return _canonical_dict_bytes(payload).decode("utf-8")


def _consensus_outcome(
    ballots: Sequence[DerivedBallot], *, participant_count: int, max_dissenters: int
) -> ConsensusOutcome:
    accepts = sum(ballot.derived_overall_vote == "accept" for ballot in ballots)
    blocker = any(ballot.ballot.blocking_objection for ballot in ballots)
    if accepts == participant_count:
        return "UNANIMOUS"
    if accepts >= 1 and accepts >= participant_count - max_dissenters and not blocker:
        return "ROUGH_CONSENSUS"
    return "CONTESTED"


def _report_lines(
    candidate: CandidateConclusion,
    ballots: Sequence[DerivedBallot],
    participants: Sequence[_Participant],
    *,
    moderator_opening: OpeningPosition | None = None,
) -> list[str]:
    by_alias = {ballot.participant_alias: ballot for ballot in ballots}
    lines: list[str] = []
    if moderator_opening is not None:
        lines.extend(
            [
                "## Moderator independent opening",
                "",
                moderator_opening.conclusion,
                "",
            ]
        )
    lines.extend(["## Council answer", "", candidate.answer, "", "## Vote matrix", ""])
    header = "| Proposition | " + " | ".join(item.alias for item in participants) + " |"
    lines.extend([header, "|---|" + "---|" * len(participants)])
    for proposition in candidate.propositions:
        votes = []
        for item in participants:
            ballot = by_alias[item.alias]
            vote = next(
                value.vote
                for value in ballot.ballot.proposition_votes
                if value.proposition_id == proposition.id
            )
            votes.append(vote)
        lines.append(f"| {proposition.id} | " + " | ".join(votes) + " |")
        lines.append(f"\nRationale for {proposition.id}: {proposition.rationale}")
    lines.extend(["", "## Dissent and blockers", ""])
    for ballot in ballots:
        if ballot.ballot.minority_report:
            lines.append(f"- {ballot.participant_alias} minority report: {ballot.ballot.minority_report}")
        if ballot.ballot.blocking_objection:
            lines.append(
                f"- {ballot.participant_alias} blocking objection: "
                f"{ballot.ballot.blocking_objection_evidence}"
            )
    if not any(
        ballot.ballot.minority_report or ballot.ballot.blocking_objection for ballot in ballots
    ):
        lines.append("- None")
    lines.extend(["", "## Unresolved questions", ""])
    lines.extend(f"- {question}" for question in candidate.unresolved_questions)
    if not candidate.unresolved_questions:
        lines.append("- None")
    lines.extend(["", "## Participant identities", ""])
    lines.extend(
        f"- {item.alias}: {item.target.runtime} / {item.target.model}"
        for item in participants
    )
    return lines
