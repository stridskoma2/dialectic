"""Thin Typer ingress: parse, securely acquire named files, call the service."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console

from .app_logging import (
    close_structured_logging,
    configure_structured_logging,
    current_structured_log_path,
    log_event,
)
from .contracts import exit_code_for
from .config import ConfigError
from .ingress import InputAcquisitionError, acquire_named_file
from .redaction import CredentialBoundaryError
from .runtime import build_service
from .schemas import DoctorReport, RunAuditReport, RunRecord, WorkspaceRecord
from .service import DialecticService
from .store import BootstrapError, InvalidRunIdError, RunNotFoundError, StateCorruptError

ServiceFactory = Callable[[], DialecticService]
_LOGGER = logging.getLogger("dialectic.cli")


class DiagnosticMode(str, Enum):
    code = "code"
    council = "council"


def create_app(service_factory: ServiceFactory = build_service) -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
    console = Console(stderr=False)

    @app.command()
    def code(
        config: Path = typer.Option(..., "--config", dir_okay=False),
        repo: Path = typer.Option(..., "--repo"),
        task_file: Path = typer.Option(..., "--task-file", dir_okay=False),
    ) -> None:
        service = service_factory()
        service.set_progress_observer(lambda record: _print_progress(console, record))
        try:
            handle = service.create_run("code")
        except BootstrapError as exc:
            console.print(str(exc))
            raise typer.Exit(2) from exc
        try:
            config_bytes = acquire_named_file(config, label="configuration")
            task_bytes = acquire_named_file(task_file, label="task")
        except InputAcquisitionError as exc:
            record = service.fail_invalid_input(handle, str(exc))
            _print_record(console, record, service.run_artifact_directory(record.run_id))
            raise typer.Exit(exit_code_for(record.status, record.failure_kind)) from exc
        record = asyncio.run(
            service.execute_code_once(
                handle,
                config_bytes=config_bytes,
                task_bytes=task_bytes,
                repository_path=repo,
            )
        )
        _print_record(
            console,
            record,
            service.run_artifact_directory(record.run_id),
            workspace=service.get_workspace(record.run_id),
        )
        raise typer.Exit(exit_code_for(record.status, record.failure_kind))

    @app.command()
    def council(
        config: Path = typer.Option(..., "--config", dir_okay=False),
        prompt_file: Path = typer.Option(..., "--prompt-file", dir_okay=False),
    ) -> None:
        service = service_factory()
        service.set_progress_observer(lambda record: _print_progress(console, record))
        try:
            handle = service.create_run("council")
        except BootstrapError as exc:
            console.print(str(exc))
            raise typer.Exit(2) from exc
        try:
            config_bytes = acquire_named_file(config, label="configuration")
            prompt_bytes = acquire_named_file(prompt_file, label="council prompt")
        except InputAcquisitionError as exc:
            record = service.fail_invalid_input(handle, str(exc))
            _print_record(console, record, service.run_artifact_directory(record.run_id))
            raise typer.Exit(exit_code_for(record.status, record.failure_kind)) from exc
        record = asyncio.run(
            service.execute_council_once(
                handle,
                config_bytes=config_bytes,
                prompt_bytes=prompt_bytes,
            )
        )
        _print_record(console, record, service.run_artifact_directory(record.run_id))
        raise typer.Exit(exit_code_for(record.status, record.failure_kind))

    @app.command()
    def status(run_id: str) -> None:
        service = service_factory()
        try:
            record = service.get_run(run_id)
            artifact_dir = service.run_artifact_directory(run_id)
            workspace = service.get_workspace(run_id)
        except (InvalidRunIdError, RunNotFoundError) as exc:
            console.print(str(exc))
            raise typer.Exit(2) from exc
        except StateCorruptError as exc:
            console.print(str(exc))
            raise typer.Exit(3) from exc
        _print_record(
            console,
            record,
            artifact_dir,
            workspace=workspace,
        )

    @app.command()
    def doctor(
        config: Path = typer.Option(..., "--config", dir_okay=False),
        mode: DiagnosticMode = typer.Option(..., "--mode", case_sensitive=False),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Validate configuration, native CLIs, authentication, and capabilities."""

        service = service_factory()
        try:
            config_bytes = acquire_named_file(config, label="configuration")
            report = asyncio.run(
                service.doctor(config_bytes=config_bytes, mode=mode.value)
            )
        except (InputAcquisitionError, ConfigError, CredentialBoundaryError) as exc:
            console.print(str(exc))
            raise typer.Exit(2) from exc
        except Exception as exc:
            console.print(f"doctor failed: {type(exc).__name__}")
            raise typer.Exit(3) from exc
        _print_doctor(console, report, json_output=json_output)
        if not report.healthy:
            raise typer.Exit(2)

    @app.command()
    def audit(
        run_id: str,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        """Audit retained run evidence without changing or repairing it."""

        service = service_factory()
        try:
            report = service.audit_run(run_id)
        except (InvalidRunIdError, RunNotFoundError) as exc:
            console.print(str(exc))
            raise typer.Exit(2) from exc
        except Exception as exc:
            console.print(f"audit failed: {type(exc).__name__}")
            raise typer.Exit(3) from exc
        _print_audit(console, report, json_output=json_output)
        if not report.valid:
            raise typer.Exit(3)

    return app


app = create_app()


def main() -> None:
    try:
        log_path = configure_structured_logging("cli")
    except Exception as exc:
        typer.echo(
            f"warning: structured application log is unavailable ({type(exc).__name__})",
            err=True,
        )
        log_path = None
    if log_path is not None:
        log_event(_LOGGER, logging.INFO, "application.started", log_path=str(log_path))
    try:
        app()
    finally:
        if log_path is not None:
            log_event(_LOGGER, logging.INFO, "application.stopped")
            close_structured_logging()


def _print_progress(console: Console, record: RunRecord) -> None:
    console.print(f"{record.run_id}  {record.phase or '-'}  {record.status}")


def _print_doctor(
    console: Console, report: DoctorReport, *, json_output: bool
) -> None:
    if json_output:
        console.print(
            report.model_dump_json(indent=2),
            highlight=False,
            markup=False,
            soft_wrap=True,
        )
        return
    console.print(f"doctor  {report.mode}  {'READY' if report.healthy else 'NOT READY'}")
    console.print(f"config sha256: {report.config_sha256}")
    console.print(f"state root: {report.state_root}", soft_wrap=True)
    for target in report.targets:
        label = f"{target.role}/{target.target_id}"
        if not target.ready:
            console.print(f"[FAIL] {label}  {target.target.runtime}: {target.diagnostic}")
            continue
        console.print(
            f"[OK] {label}  {target.target.runtime} {target.cli_version}  "
            f"{target.prompt_transport}/{target.process_lifecycle}"
        )
        console.print(f"     executable: {target.resolved_executable}", soft_wrap=True)
        console.print(
            f"     fixture: {target.adapter_fixture_version}; "
            f"attestation: {target.capability_attestation_sha256}"
        )


def _print_audit(
    console: Console, report: RunAuditReport, *, json_output: bool
) -> None:
    if json_output:
        console.print(
            report.model_dump_json(indent=2),
            highlight=False,
            markup=False,
            soft_wrap=True,
        )
        return
    result = "VALID" if report.valid else "INVALID"
    completeness = "complete" if report.complete else "incomplete"
    console.print(f"{report.run_id}  {report.status or '-'}  {result} ({completeness})")
    console.print(
        f"checked: {report.files_checked} run files, {report.events_checked} events, "
        f"{report.attempts_checked} attempts, {report.bytes_checked} bytes"
    )
    if report.manifest_sha256 is not None:
        console.print(f"evidence manifest sha256: {report.manifest_sha256}")
    for issue in report.issues:
        location = f" [{issue.path}]" if issue.path is not None else ""
        console.print(f"{issue.severity.upper()} {issue.code}{location}: {issue.detail}")


def _print_record(
    console: Console,
    record: RunRecord,
    artifact_dir: Path,
    *,
    workspace: WorkspaceRecord | None = None,
) -> None:
    console.print(f"{record.run_id}  {record.status}")
    if record.failure_kind is not None:
        console.print(f"failure: {record.failure_kind}: {record.failure_detail}")
    console.print(f"artifacts: {artifact_dir}", soft_wrap=True)
    log_path = current_structured_log_path()
    if log_path is not None:
        console.print(f"application log: {log_path}", soft_wrap=True)
    if workspace is not None and workspace.dialectic_worktree is not None:
        console.print(f"isolated worktree: {workspace.dialectic_worktree}", soft_wrap=True)
        console.print(f"branch: {workspace.dialectic_branch}")
        console.print(
            "original repository unchanged: checked-out files, index, branch, HEAD, "
            "pre-existing branches, and main"
        )
        console.print(
            "shared Git metadata and objects added: linked-worktree metadata, the "
            "Dialectic branch, commits, and objects"
        )
        console.print("cleanup (run from the original repository; never automatic):")
        console.print(f'  git worktree remove "{workspace.dialectic_worktree}"')
        console.print(f"  git branch -D {workspace.dialectic_branch}")
        console.print("  git worktree prune")


if __name__ == "__main__":
    main()
