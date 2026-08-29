"""Bounded offline Code Once orchestration.

The workflow in this module owns ordering, evidence, Git validation, reviewer
fan-out, and the single repair decision.  Native process policy remains behind
``AgentAdapter`` so the Slice 1 suite can run without provider CLIs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import AgentAdapter, AgentProcessError, AgentRegistry, ModelMismatchError
from .capabilities import (
    BindingBarrier,
    CapabilityEvidenceError,
    CapabilityFixture,
    build_capability_binding,
    instantiate_capability_template,
    validate_binding_identities,
)
from .contracts import ARTIFACT_SCHEMA_VERSION, TOOL_VERSION, CodeOutcome, FailureKind
from .config import validate_model_bounds
from .git_workspace import (
    ChangeValidator,
    GitCommandError,
    GitOutputLimitError,
    GitRunner,
    GitWorkflowError,
    GitWorkspace,
    LinkedWorkspace,
    RepositoryBaseline,
    ValidatedChange,
)
from .locking import RepositoryBusyError, RepositoryLock
from .native_adapters import (
    NativeInvocationEvidence,
    NativePreflightError,
    NativeTurnError,
)
from .output import OutputError, extract_model_payload
from .schemas import (
    AgentRequest,
    AgentRequestArtifact,
    AgentResponse,
    AgentTarget,
    CapabilityAttestationArtifact,
    CapabilityBindingArtifact,
    CapabilityProbeResult,
    DriverRepairReport,
    FeedbackArtifact,
    NormalizedFinding,
    PreflightResult,
    ReviewManifest,
    ReviewReport,
    ReviewReportArtifact,
    RunRecord,
    StreamCaptureResult,
    TargetPreflightArtifact,
    TurnAttemptArtifact,
    WorkspaceRecord,
)
from .service import DialecticFailure, ExecutionContext, WorkflowTimedOut
from .store import canonical_json_bytes
from .turn_workspace import (
    TurnWorkspace,
    TurnWorkspaceCleanupError,
    TurnWorkspaceError,
)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class TurnFailure(RuntimeError):
    def __init__(self, kind: FailureKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _GateAEvidence:
    preflight: TargetPreflightArtifact
    preflight_bytes: bytes
    preflight_sha256: str
    preflight_relative_path: str
    attestation: CapabilityAttestationArtifact
    attestation_bytes: bytes
    fixture: CapabilityFixture


@dataclass(frozen=True, slots=True)
class _ReviewerContext:
    alias: str
    target: AgentTarget
    adapter: AgentAdapter
    gate_a: _GateAEvidence
    binding: CapabilityBindingArtifact
    binding_sha256: str
    neutral_directory: Path
    prompt: str
    packet_sha256: str


@dataclass(frozen=True, slots=True)
class _TurnResult:
    response: AgentResponse
    attempt: TurnAttemptArtifact


class CodeOnceOrchestrator:
    """Execute exactly one implementation, one review cohort, and one repair."""

    def __init__(
        self,
        *,
        driver_adapter: AgentAdapter,
        reviewer_adapters: Mapping[str, AgentAdapter] | None = None,
        change_validator_factory: type[ChangeValidator] = ChangeValidator,
    ) -> None:
        self.driver_adapter = driver_adapter
        self.reviewer_adapters = dict(reviewer_adapters or {})
        self.change_validator_factory = change_validator_factory

    async def __call__(self, context: ExecutionContext) -> RunRecord:
        if context.repository_path is None:
            raise DialecticFailure("INVALID_INPUT", "code mode requires a repository path")
        driver_target, reviewers = AgentRegistry.code_targets(context.config)
        gate_a = await self._preflight_targets(context, driver_target, reviewers)

        hooks = context.handle.path / "controller-hooks"
        hooks.mkdir(mode=0o700)
        runner = GitRunner(hooks, timeout_seconds=context.config.limits.preflight_seconds)
        git = GitWorkspace(runner, context.service.store.state_root)
        try:
            baseline = git.preflight(context.repository_path)
        except GitWorkflowError as exc:
            raise DialecticFailure(exc.kind, exc.detail) from exc
        except (GitCommandError, GitOutputLimitError) as exc:
            raise DialecticFailure("PREFLIGHT_FAILED", "Git preflight command failed") from exc

        lock = RepositoryLock(
            context.service.store.state_root / "locks",
            baseline.identity,
            context.handle.run_id,
        )
        try:
            lock.acquire()
        except RepositoryBusyError as exc:
            raise DialecticFailure("REPOSITORY_BUSY", str(exc)) from exc
        try:
            try:
                locked_baseline = git.preflight(context.repository_path)
            except GitWorkflowError as exc:
                raise DialecticFailure(exc.kind, exc.detail) from exc
            if locked_baseline.identity != baseline.identity:
                raise DialecticFailure(
                    "PREFLIGHT_FAILED",
                    "repository identity changed while acquiring its advisory lock",
                )
            return await self._run_locked(
                context,
                runner,
                git,
                locked_baseline,
                gate_a,
                driver_target,
                reviewers,
            )
        finally:
            lock.release()

    async def _preflight_targets(
        self,
        context: ExecutionContext,
        driver_target: AgentTarget,
        reviewers: list[tuple[str, AgentTarget]],
    ) -> dict[tuple[str, str], _GateAEvidence]:
        requests: list[tuple[str, str, str, AgentTarget, AgentAdapter, str]] = [
            ("driver", "driver", "driver", driver_target, self.driver_adapter, "driver-write")
        ]
        for index, (reviewer_id, target) in enumerate(reviewers):
            adapter = self.reviewer_adapters.get(reviewer_id)
            if adapter is None:
                adapter = self.driver_adapter if target == driver_target else None
            if adapter is None:
                raise DialecticFailure(
                    "PREFLIGHT_FAILED", f"no adapter is configured for reviewer {reviewer_id}"
                )
            alias = f"reviewer-{chr(ord('a') + index)}"
            requests.append(
                ("reviewer", reviewer_id, alias, target, adapter, "packet-only")
            )

        async def one(
            role: str,
            lookup_id: str,
            artifact_target_id: str,
            target: AgentTarget,
            adapter: AgentAdapter,
            access_mode: str,
        ) -> tuple[tuple[str, str], _GateAEvidence]:
            try:
                result = await asyncio.wait_for(
                    adapter.preflight(target),
                    timeout=context.config.limits.preflight_seconds,
                )
            except Exception as exc:
                raise DialecticFailure(
                    "PREFLIGHT_FAILED",
                    f"target preflight failed for {lookup_id}: "
                    f"{_bounded_preflight_diagnostic(exc)}",
                ) from exc
            if not result.authentication_verified or result.target != target:
                raise DialecticFailure(
                    "PREFLIGHT_FAILED", f"target preflight failed for {lookup_id}"
                )
            evidence = self._persist_gate_a(
                context,
                adapter=adapter,
                role=role,
                target_id=artifact_target_id,
                target=target,
                result=result,
                access_mode=access_mode,
            )
            return (role, lookup_id), evidence

        pairs = await asyncio.gather(*(one(*request) for request in requests))
        return dict(pairs)

    def _persist_gate_a(
        self,
        context: ExecutionContext,
        *,
        adapter: AgentAdapter,
        role: str,
        target_id: str,
        target: AgentTarget,
        result: PreflightResult,
        access_mode: str,
    ) -> _GateAEvidence:
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
            return _GateAEvidence(
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
            prompt_transport="acp-stdio" if target.runtime == "grok-build" else "stdin",
            process_lifecycle="per-turn",
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
        return _GateAEvidence(
            preflight=preflight,
            preflight_bytes=preflight_bytes,
            preflight_sha256=preflight_sha,
            preflight_relative_path=relative,
            attestation=attestation,
            attestation_bytes=attestation_bytes,
            fixture=fixture,
        )

    async def _run_locked(
        self,
        context: ExecutionContext,
        runner: GitRunner,
        git: GitWorkspace,
        baseline: RepositoryBaseline,
        gate_a: dict[tuple[str, str], _GateAEvidence],
        driver_target: AgentTarget,
        reviewer_specs: list[tuple[str, AgentTarget]],
    ) -> RunRecord:
        workspace_record = self._workspace_record(baseline)
        self._write_workspace(context, workspace_record)
        context.service.advance_phase(context.handle, "WORKTREE_SETUP")
        try:
            workspace = git.create_linked_worktree(baseline, context.handle.run_id)
        except GitWorkflowError as exc:
            raise DialecticFailure(exc.kind, exc.detail) from exc
        workspace_record = workspace_record.model_copy(
            update={
                "dialectic_branch": workspace.branch,
                "dialectic_worktree": str(workspace.path),
            }
        )
        self._write_workspace(context, workspace_record)

        initial_scratch = TurnWorkspace.create(workspace.path)
        driver_gate = gate_a[("driver", "driver")]
        initial_binding, initial_binding_sha, initial_paths = self._bind_driver(
            context,
            workspace,
            initial_scratch,
            driver_gate,
            "initial",
            adapter=self.driver_adapter,
        )

        try:
            async with asyncio.timeout(context.config.limits.code_run_seconds):
                return await self._run_model_flow(
                    context=context,
                    runner=runner,
                    workspace=workspace,
                    workspace_record=workspace_record,
                    driver_target=driver_target,
                    reviewer_specs=reviewer_specs,
                    gate_a=gate_a,
                    initial_scratch=initial_scratch,
                    initial_binding=initial_binding,
                    initial_binding_sha=initial_binding_sha,
                    initial_paths=initial_paths,
                )
        except TimeoutError as exc:
            raise WorkflowTimedOut("Code Once workflow wall clock expired") from exc

    async def _run_model_flow(
        self,
        *,
        context: ExecutionContext,
        runner: GitRunner,
        workspace: LinkedWorkspace,
        workspace_record: WorkspaceRecord,
        driver_target: AgentTarget,
        reviewer_specs: list[tuple[str, AgentTarget]],
        gate_a: dict[tuple[str, str], _GateAEvidence],
        initial_scratch: TurnWorkspace,
        initial_binding: CapabilityBindingArtifact,
        initial_binding_sha: str,
        initial_paths: Mapping[str, Path],
    ) -> RunRecord:
        prompt = _initial_driver_prompt(context.input_text, workspace.path)
        self._require_packet_bound(context, prompt, output_schema=None, target_id="driver")
        context.service.mark_model_work_started(context.handle)
        context.service.advance_phase(context.handle, "DRIVER_INITIAL")
        try:
            self._validate_launch_evidence(
                context,
                gate_a=gate_a[("driver", "driver")],
                binding=initial_binding,
                binding_sha256=initial_binding_sha,
                binding_relative_path=(
                    "audit/capabilities/driver/driver/initial.binding.json"
                ),
                dynamic_paths=initial_paths,
            )
            initial_turn = await self._invoke_turn(
                context,
                adapter=self.driver_adapter,
                target=driver_target,
                gate_a=gate_a[("driver", "driver")],
                binding_sha256=initial_binding_sha,
                role="driver",
                target_id="driver",
                phase="initial",
                operation="start",
                prompt=prompt,
                output_schema=None,
                working_directory=workspace.path,
                access_mode="driver-write",
                failure_kind="DRIVER_FAILED",
            )
        finally:
            self._cleanup_driver_scratch(initial_scratch, context)
        if not initial_turn.response.session_id:
            raise DialecticFailure(
                "DRIVER_FAILED", "initial driver response lacks a resumable session id"
            )

        context.service.advance_phase(context.handle, "INITIAL_VALIDATION")
        validator = self.change_validator_factory(
            runner=runner,
            store=context.service.store,
            handle=context.handle,
            workspace=workspace,
            limits=context.config.limits,
        )
        initial = self._validate_change(validator.validate_initial)
        workspace_record = workspace_record.model_copy(
            update={
                "review_sha": initial.head_sha,
                "initial_diff_sha256": initial.complete_diff_sha256,
            }
        )
        self._write_workspace(context, workspace_record)

        context.service.advance_phase(context.handle, "REVIEWERS")
        reviewer_contexts = self._prepare_reviewers(
            context, workspace, reviewer_specs, gate_a, initial
        )
        reports = await self._run_reviewers(context, reviewer_contexts, initial)
        total_findings = sum(len(report.report.findings) for report in reports)
        if total_findings > context.config.limits.max_total_findings:
            raise DialecticFailure(
                "REVIEW_FAILED", "aggregate review findings exceed max_total_findings"
            )
        manifest = ReviewManifest(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            base_sha=workspace.baseline.base_sha,
            review_sha=initial.head_sha,
            diff_sha256=initial.complete_diff_sha256,
            reviewer_aliases=[reviewer.alias for reviewer in reviewer_contexts],
            reports=[f"reviews/{reviewer.alias}.json" for reviewer in reviewer_contexts],
        )
        context.service.store.write_artifact(context.handle, "reviews/manifest.json", manifest)

        if total_findings == 0:
            context.service.store.write_artifact(
                context.handle, "git/final.diff", initial.complete_diff
            )
            context.service.store.write_artifact(
                context.handle,
                "git/final.diff.sha256",
                (initial.complete_diff_sha256 + "\n").encode("ascii"),
            )
            workspace_record = workspace_record.model_copy(
                update={
                    "final_sha": initial.head_sha,
                    "final_diff_sha256": initial.complete_diff_sha256,
                }
            )
            self._write_workspace(context, workspace_record)
            context.service.advance_phase(context.handle, "REPORTING")
            return context.service.finalize_code(
                context.handle,
                "COMPLETED_NO_FINDINGS",
                artifact_paths=_code_artifact_paths(findings=False),
            )

        context.service.advance_phase(context.handle, "FEEDBACK")
        feedback = _build_feedback(initial.head_sha, reports)
        context.service.store.write_artifact(context.handle, "feedback.json", feedback)
        feedback_prompt = _repair_prompt(feedback)
        repair_schema = DriverRepairReport.model_json_schema()
        self._require_packet_bound(
            context,
            feedback_prompt,
            output_schema=repair_schema,
            target_id="driver repair",
        )

        repair_scratch = TurnWorkspace.create(workspace.path)
        repair_binding, repair_binding_sha, repair_paths = self._bind_driver(
            context,
            workspace,
            repair_scratch,
            gate_a[("driver", "driver")],
            "repair",
            adapter=self.driver_adapter,
        )
        context.service.advance_phase(context.handle, "DRIVER_REPAIR")
        try:
            self._validate_launch_evidence(
                context,
                gate_a=gate_a[("driver", "driver")],
                binding=repair_binding,
                binding_sha256=repair_binding_sha,
                binding_relative_path=(
                    "audit/capabilities/driver/driver/repair.binding.json"
                ),
                dynamic_paths=repair_paths,
            )
            repair_turn = await self._invoke_turn(
                context,
                adapter=self.driver_adapter,
                target=driver_target,
                gate_a=gate_a[("driver", "driver")],
                binding_sha256=repair_binding_sha,
                role="driver",
                target_id="driver",
                phase="repair",
                operation="resume",
                session_id=initial_turn.response.session_id,
                prompt=feedback_prompt,
                output_schema=repair_schema,
                working_directory=workspace.path,
                access_mode="driver-write",
                failure_kind="REPAIR_FAILED",
            )
            try:
                repair_report = extract_model_payload(
                    repair_turn.response.text,
                    DriverRepairReport,
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                    context={
                        "finding_keys": [finding.finding_key for finding in feedback.findings]
                    },
                )
            except OutputError as exc:
                raise DialecticFailure("REPAIR_FAILED", "invalid driver repair report") from exc
        finally:
            self._cleanup_driver_scratch(repair_scratch, context)

        context.service.advance_phase(context.handle, "FINAL_VALIDATION")
        final = self._validate_change(lambda: validator.validate_repair(initial.head_sha))
        fixed = any(item.outcome == "fixed" for item in repair_report.dispositions)
        if fixed and not final.repair_delta:
            raise DialecticFailure(
                "REPAIR_FAILED", "a fixed disposition requires a non-empty repair delta"
            )
        workspace_record = workspace_record.model_copy(
            update={
                "final_sha": final.head_sha,
                "repair_delta_sha256": final.repair_delta_sha256,
                "final_diff_sha256": final.complete_diff_sha256,
            }
        )
        self._write_workspace(context, workspace_record)
        unresolved = [
            item.finding_key
            for item in repair_report.dispositions
            if item.outcome == "not_fixed"
        ]
        rebuttals = [
            f"{item.finding_key}: {item.explanation}"
            for item in repair_report.dispositions
            if item.outcome == "rejected_with_evidence"
        ]
        outcome: CodeOutcome
        if unresolved:
            outcome = "COMPLETED_WITH_UNRESOLVED_FINDINGS"
        elif final.repair_delta:
            outcome = "COMPLETED_AFTER_REPAIR"
        else:
            outcome = "COMPLETED_WITH_REBUTTALS"
        context.service.advance_phase(context.handle, "REPORTING")
        return context.service.finalize_code(
            context.handle,
            outcome,
            unresolved_items=unresolved,
            artifact_paths=_code_artifact_paths(findings=True),
            markdown_notes=[
                "The repaired code has not been re-reviewed.",
                *(["Rebuttals:", *rebuttals] if rebuttals else []),
            ],
        )

    def _prepare_reviewers(
        self,
        context: ExecutionContext,
        workspace: LinkedWorkspace,
        reviewer_specs: list[tuple[str, AgentTarget]],
        gate_a: dict[tuple[str, str], _GateAEvidence],
        initial: ValidatedChange,
    ) -> list[_ReviewerContext]:
        assert context.config.reviewers is not None
        review_schema = ReviewReport.model_json_schema()
        core = {
            "task": context.input_text,
            "base_sha": workspace.baseline.base_sha,
            "review_sha": initial.head_sha,
            "diff_sha256": initial.complete_diff_sha256,
            "diff": initial.complete_diff.decode("utf-8", errors="strict"),
            "output_schema": review_schema,
        }
        core_bytes = _canonical_dict_bytes(core)
        context.service.store.write_artifact(
            context.handle,
            "reviews/core.sha256",
            (hashlib.sha256(core_bytes).hexdigest() + "\n").encode("ascii"),
        )
        reviewers_root = context.handle.path / "reviewer-workspaces"
        reviewers_root.mkdir(mode=0o700)
        barrier = BindingBarrier(
            f"reviewer-{chr(ord('a') + index)}"
            for index in range(len(reviewer_specs))
        )
        prepared: list[_ReviewerContext] = []
        for index, ((reviewer_id, target), spec) in enumerate(
            zip(reviewer_specs, context.config.reviewers, strict=True)
        ):
            alias = f"reviewer-{chr(ord('a') + index)}"
            packet = {"core": core, "lens": spec.lens}
            packet_bytes = _canonical_dict_bytes(packet)
            self._require_packet_bound(
                context,
                packet_bytes.decode("utf-8"),
                output_schema=review_schema,
                target_id=alias,
            )
            neutral = reviewers_root / alias
            neutral.mkdir(mode=0o700)
            evidence = gate_a[("reviewer", reviewer_id)]
            adapter = self.reviewer_adapters.get(reviewer_id)
            if adapter is None:
                adapter = self.driver_adapter
            dynamic_paths = {"neutral_role_dir": neutral}
            concrete = _concrete_profile(evidence.fixture, dynamic_paths)
            binding = build_capability_binding(
                binding_id=f"{context.handle.run_id}:{alias}:review",
                role="reviewer",
                target_id=alias,
                access_mode="packet-only",
                target_preflight_bytes=evidence.preflight_bytes,
                attestation_bytes=evidence.attestation_bytes,
                attestation=evidence.attestation,
                fixture=evidence.fixture,
                dynamic_paths=dynamic_paths,
                supplied_concrete_profile=concrete,
            )
            binding_sha = context.service.store.write_artifact(
                context.handle,
                f"audit/capabilities/reviewer/{alias}/review.binding.json",
                binding,
            )
            _authorize_native_binding(adapter, binding, concrete, dynamic_paths)
            barrier.add(alias, binding)
            prepared.append(
                _ReviewerContext(
                    alias=alias,
                    target=target,
                    adapter=adapter,
                    gate_a=evidence,
                    binding=binding,
                    binding_sha256=binding_sha,
                    neutral_directory=neutral,
                    prompt=packet_bytes.decode("utf-8"),
                    packet_sha256=hashlib.sha256(packet_bytes).hexdigest(),
                )
            )
        barrier.authorize_launch()
        return prepared

    async def _run_reviewers(
        self,
        context: ExecutionContext,
        reviewers: Sequence[_ReviewerContext],
        initial: ValidatedChange,
    ) -> list[ReviewReportArtifact]:
        peer_failure = asyncio.Event()

        async def one(reviewer: _ReviewerContext) -> ReviewReportArtifact:
            self._validate_launch_evidence(
                context,
                gate_a=reviewer.gate_a,
                binding=reviewer.binding,
                binding_sha256=reviewer.binding_sha256,
                binding_relative_path=(
                    f"audit/capabilities/reviewer/{reviewer.alias}/review.binding.json"
                ),
                dynamic_paths={"neutral_role_dir": reviewer.neutral_directory},
            )
            turn = await self._invoke_turn(
                context,
                adapter=reviewer.adapter,
                target=reviewer.target,
                gate_a=reviewer.gate_a,
                binding_sha256=reviewer.binding_sha256,
                role="reviewer",
                target_id=reviewer.alias,
                phase="review",
                operation="start",
                prompt=reviewer.prompt,
                output_schema=ReviewReport.model_json_schema(),
                working_directory=reviewer.neutral_directory,
                access_mode="packet-only",
                failure_kind="REVIEW_FAILED",
                peer_failure=peer_failure,
            )
            try:
                packet = json.loads(reviewer.prompt)
                core = packet["core"]
                report = extract_model_payload(
                    turn.response.text,
                    ReviewReport,
                    max_chars=context.config.limits.max_model_field_chars,
                    max_items=context.config.limits.max_model_list_items,
                    context={
                        "base_sha": core["base_sha"],
                        "head_sha": core["review_sha"],
                        "max_findings": context.config.limits.max_findings_per_reviewer,
                    },
                )
            except (OutputError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise DialecticFailure(
                    "REVIEW_FAILED", f"invalid review report from {reviewer.alias}"
                ) from exc
            artifact = ReviewReportArtifact(
                artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
                tool_version=TOOL_VERSION,
                reviewer_alias=reviewer.alias,
                target=reviewer.target,
                packet_sha256=reviewer.packet_sha256,
                report=report,
            )
            context.service.store.write_artifact(
                context.handle, f"reviews/{reviewer.alias}.json", artifact
            )
            return artifact

        tasks = [asyncio.create_task(one(reviewer)) for reviewer in reviewers]
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_EXCEPTION
                )
                failure = next(
                    (task.exception() for task in done if task.exception() is not None),
                    None,
                )
                if failure is not None:
                    peer_failure.set()
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if isinstance(failure, DialecticFailure):
                        raise failure
                    raise DialecticFailure("REVIEW_FAILED", "reviewer cohort failed") from failure
            return [task.result() for task in tasks]
        except asyncio.CancelledError:
            peer_failure.set()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise

    async def _invoke_turn(
        self,
        context: ExecutionContext,
        *,
        adapter: AgentAdapter,
        target: AgentTarget,
        gate_a: _GateAEvidence,
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
    ) -> _TurnResult:
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
            response = _redacted_response(response, context)
        except asyncio.TimeoutError as exc:
            termination = "timeout"
            attempt_failure = failure_kind
            diagnostic = "agent turn reached its individual timeout"
            caught = exc
        except asyncio.CancelledError as exc:
            termination = "peer-failure" if peer_failure and peer_failure.is_set() else "cancelled"
            attempt_failure = failure_kind
            diagnostic = "agent turn cancelled after peer failure" if peer_failure else "agent turn cancelled"
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

        evidence = _take_native_invocation_evidence(adapter)
        stream_out = _empty_stream(context.config.limits.max_agent_stdout_bytes)
        stream_err = _empty_stream(context.config.limits.max_agent_stderr_bytes)
        completed = datetime.now(UTC)
        if evidence is None:
            process_origin = "spawned-for-attempt" if process_started else "none"
            process_lifecycle = gate_a.preflight.process_lifecycle
            process_unit_id = (
                _process_unit_id(context.handle.run_id, role, target_id, phase)
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
            if (
                isinstance(caught, asyncio.CancelledError)
                and process_origin == "none"
            ):
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
        return _TurnResult(response=response, attempt=attempt)

    def _bind_driver(
        self,
        context: ExecutionContext,
        workspace: LinkedWorkspace,
        scratch: TurnWorkspace,
        evidence: _GateAEvidence,
        phase: str,
        *,
        adapter: AgentAdapter,
    ) -> tuple[CapabilityBindingArtifact, str, Mapping[str, Path]]:
        dynamic_paths = {
            "isolated_worktree": workspace.path,
            "git_common_dir": workspace.baseline.common_directory,
            "original_worktree": workspace.baseline.original_worktree,
            "state_root": context.service.store.state_root,
            "turn_scratch_root": scratch.root,
            "turn_scratch_control": scratch.control,
            "turn_scratch_tmp": scratch.temporary,
        }
        concrete = _concrete_profile(evidence.fixture, dynamic_paths)
        try:
            binding = build_capability_binding(
                binding_id=f"{context.handle.run_id}:driver:{phase}",
                role="driver",
                target_id="driver",
                access_mode="driver-write",
                target_preflight_bytes=evidence.preflight_bytes,
                attestation_bytes=evidence.attestation_bytes,
                attestation=evidence.attestation,
                fixture=evidence.fixture,
                dynamic_paths=dynamic_paths,
                supplied_concrete_profile=concrete,
            )
        except CapabilityEvidenceError as exc:
            raise DialecticFailure("PREFLIGHT_FAILED", "driver capability binding failed") from exc
        sha = context.service.store.write_artifact(
            context.handle,
            f"audit/capabilities/driver/driver/{phase}.binding.json",
            binding,
        )
        _authorize_native_binding(adapter, binding, concrete, dynamic_paths)
        return binding, sha, dynamic_paths

    @staticmethod
    def _cleanup_driver_scratch(scratch: TurnWorkspace, context: ExecutionContext) -> None:
        try:
            scratch.verify_and_cleanup(context.config.limits)
        except TurnWorkspaceCleanupError as exc:
            raise DialecticFailure(
                "PROCESS_CLEANUP_FAILED", "reserved driver workspace cleanup failed"
            ) from exc
        except TurnWorkspaceError as exc:
            raise DialecticFailure(
                "INTERNAL_ERROR", "reserved driver workspace validation failed"
            ) from exc

    @staticmethod
    def _require_packet_bound(
        context: ExecutionContext,
        prompt: str,
        *,
        output_schema: dict[str, Any] | None,
        target_id: str,
    ) -> None:
        packet = {
            "prompt": prompt,
            "output_schema": output_schema,
        }
        if len(_canonical_dict_bytes(packet)) > context.config.limits.max_packet_bytes:
            raise DialecticFailure(
                "PACKET_TOO_LARGE",
                f"outbound packet exceeds max_packet_bytes for {target_id}",
            )

    @staticmethod
    def _validate_launch_evidence(
        context: ExecutionContext,
        *,
        gate_a: _GateAEvidence,
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

    @staticmethod
    def _validate_change(operation):  # type: ignore[no-untyped-def]
        try:
            return operation()
        except GitWorkflowError as exc:
            raise DialecticFailure(exc.kind, exc.detail) from exc
        except (GitCommandError, GitOutputLimitError) as exc:
            raise DialecticFailure("INTERNAL_ERROR", "Git validation command failed") from exc

    @staticmethod
    def _workspace_record(baseline: RepositoryBaseline) -> WorkspaceRecord:
        return WorkspaceRecord(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            repo_common_dir=str(baseline.common_directory),
            repo_filesystem_identity=baseline.identity.filesystem_identity,
            repo_lock_identity_sha256=baseline.identity.lock_identity_sha256,
            original_worktree=str(baseline.original_worktree),
            original_branch=baseline.original_branch,
            base_sha=baseline.base_sha,
            dialectic_branch=None,
            dialectic_worktree=None,
            review_sha=None,
            final_sha=None,
            initial_diff_sha256=None,
            repair_delta_sha256=None,
            final_diff_sha256=None,
        )

    @staticmethod
    def _write_workspace(context: ExecutionContext, record: WorkspaceRecord) -> None:
        context.service.store.write_artifact(
            context.handle, "git/workspace.json", record, immutable=False
        )


def _build_feedback(
    review_sha: str, reports: Sequence[ReviewReportArtifact]
) -> FeedbackArtifact:
    findings: list[NormalizedFinding] = []
    for report in sorted(reports, key=lambda item: item.reviewer_alias):
        for index, finding in enumerate(report.report.findings, start=1):
            findings.append(
                NormalizedFinding(
                    finding_key=f"{report.reviewer_alias}/{index:03d}",
                    reviewer_alias=report.reviewer_alias,
                    source_finding_id=finding.id,
                    finding=finding,
                )
            )
    return FeedbackArtifact(
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        tool_version=TOOL_VERSION,
        review_sha=review_sha,
        findings=findings,
    )


def _authorize_native_binding(
    adapter: AgentAdapter,
    binding: CapabilityBindingArtifact,
    concrete_profile: Mapping[str, Any],
    dynamic_paths: Mapping[str, Path],
) -> None:
    binder = getattr(adapter, "bind_capability", None)
    if callable(binder):
        binder(binding, concrete_profile, dynamic_paths)


def _take_native_invocation_evidence(
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


def _bounded_preflight_diagnostic(error: BaseException) -> str:
    detail = str(error) if isinstance(error, NativePreflightError) else type(error).__name__
    encoded = detail.encode("utf-8", errors="replace")[:1024]
    return encoded.decode("utf-8", errors="ignore") or "native preflight failed"


def _initial_driver_prompt(task: str, worktree: Path) -> str:
    return (
        "Perform one bounded implementation pass and then stop.\n"
        f"Work only in this isolated worktree: {worktree}\n"
        "Implement the task, run narrow checks you consider appropriate, and summarize your work.\n"
        "This fresh linked worktree does not contain ignored local artifacts such as .venv, "
        "node_modules, build caches, or .env. Do not repair environment setup as part of the task.\n"
        "Do not create build output or caches.\n\n"
        f"Task:\n{task}"
    )


def _repair_prompt(feedback: FeedbackArtifact) -> str:
    model_packet = {
        "review_sha": feedback.review_sha,
        "no_re_review": True,
        "instructions": [
            "Inspect every finding.",
            "Modify the isolated worktree where appropriate.",
            "Return exactly one disposition for every finding_key.",
            "Stop after this repair pass.",
        ],
        "findings": [finding.model_dump(mode="json") for finding in feedback.findings],
        "output_schema": DriverRepairReport.model_json_schema(),
    }
    return _canonical_dict_bytes(model_packet).decode("utf-8")


def _concrete_profile(
    fixture: CapabilityFixture, dynamic_paths: Mapping[str, Path]
) -> dict[str, Any]:
    return instantiate_capability_template(fixture, dynamic_paths)


def _empty_stream(limit: int) -> StreamCaptureResult:
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


def _process_unit_id(run_id: str, role: str, target_id: str, phase: str) -> str:
    digest = hashlib.sha256(
        f"{run_id}\0{role}\0{target_id}\0{phase}".encode("utf-8")
    ).digest()[:10]
    import base64

    return base64.b32encode(digest).decode("ascii").lower()


def _redacted_response(response: AgentResponse, context: ExecutionContext) -> AgentResponse:
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


def _canonical_dict_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


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


def _code_artifact_paths(*, findings: bool) -> dict[str, str]:
    paths = {
        "events": "events.jsonl",
        "run": "run.json",
        "workspace": "git/workspace.json",
        "initial_diff": "git/initial.diff",
        "final_diff": "git/final.diff",
        "reviews": "reviews/manifest.json",
    }
    if findings:
        paths.update(
            {
                "feedback": "feedback.json",
                "repair_delta": "git/repair.delta.diff",
            }
        )
    return paths
