"""Bounded offline Code Once orchestration.

The workflow in this module owns ordering, evidence, Git validation, reviewer
fan-out, and the single repair decision.  Native process policy remains behind
``AgentAdapter`` so the Slice 1 suite can run without provider CLIs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import AgentAdapter, AgentRegistry
from .capabilities import (
    BindingBarrier,
    CapabilityEvidenceError,
    build_capability_binding,
)
from .contracts import ARTIFACT_SCHEMA_VERSION, TOOL_VERSION, CodeOutcome
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
from .output import OutputError, extract_model_payload
from .schemas import (
    AgentTarget,
    CapabilityBindingArtifact,
    DriverRepairReport,
    FeedbackArtifact,
    NormalizedFinding,
    ReviewManifest,
    ReviewReport,
    ReviewReportArtifact,
    RunRecord,
    WorkspaceRecord,
)
from .service import DialecticFailure, ExecutionContext, WorkflowTimedOut
from .turn_workspace import (
    TurnWorkspace,
    TurnWorkspaceCleanupError,
    TurnWorkspaceError,
)
from .workflow_evidence import (
    GateAEvidence as _GateAEvidence,
    WorkflowEvidenceSupport,
    authorize_native_binding as _authorize_native_binding,
    bounded_preflight_diagnostic as _bounded_preflight_diagnostic,
    canonical_mapping_bytes as _canonical_dict_bytes,
    concrete_profile as _concrete_profile,
    require_packet_bound,
)


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
        self._evidence = WorkflowEvidenceSupport()

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
            evidence = self._evidence.persist_gate_a(
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
        require_packet_bound(context, prompt, output_schema=None, target_id="driver")
        context.service.mark_model_work_started(context.handle)
        context.service.advance_phase(context.handle, "DRIVER_INITIAL")
        try:
            self._evidence.validate_launch_evidence(
                context,
                gate_a=gate_a[("driver", "driver")],
                binding=initial_binding,
                binding_sha256=initial_binding_sha,
                binding_relative_path=(
                    "audit/capabilities/driver/driver/initial.binding.json"
                ),
                dynamic_paths=initial_paths,
            )
            initial_turn = await self._evidence.invoke_turn(
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
                markdown_notes=[
                    "Repair turn: not performed; reviewers returned no findings.",
                    "Re-review: not applicable.",
                ],
            )

        context.service.advance_phase(context.handle, "FEEDBACK")
        feedback = _build_feedback(initial.head_sha, reports)
        context.service.store.write_artifact(context.handle, "feedback.json", feedback)
        feedback_prompt = _repair_prompt(feedback)
        repair_schema = DriverRepairReport.model_json_schema()
        require_packet_bound(
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
            self._evidence.validate_launch_evidence(
                context,
                gate_a=gate_a[("driver", "driver")],
                binding=repair_binding,
                binding_sha256=repair_binding_sha,
                binding_relative_path=(
                    "audit/capabilities/driver/driver/repair.binding.json"
                ),
                dynamic_paths=repair_paths,
            )
            repair_turn = await self._evidence.invoke_turn(
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
                "Repair turn: performed.",
                "Re-review: not performed; the post-repair state has not been re-reviewed.",
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
            require_packet_bound(
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
            self._evidence.validate_launch_evidence(
                context,
                gate_a=reviewer.gate_a,
                binding=reviewer.binding,
                binding_sha256=reviewer.binding_sha256,
                binding_relative_path=(
                    f"audit/capabilities/reviewer/{reviewer.alias}/review.binding.json"
                ),
                dynamic_paths={"neutral_role_dir": reviewer.neutral_directory},
            )
            turn = await self._evidence.invoke_turn(
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
