"""Thin Typer ingress: parse, securely acquire named files, call the service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import typer
from rich.console import Console

from .contracts import exit_code_for
from .ingress import InputAcquisitionError, acquire_named_file
from .runtime import build_service
from .service import DialecticService
from .store import BootstrapError, InvalidRunIdError, RunNotFoundError, StateCorruptError

ServiceFactory = Callable[[], DialecticService]


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
        _print_record(console, record, service.run_artifact_directory(record.run_id))
        raise typer.Exit(exit_code_for(record.status, record.failure_kind))

    @app.command()
    def council(
        config: Path = typer.Option(..., "--config", dir_okay=False),
        prompt_file: Path = typer.Option(..., "--prompt-file", dir_okay=False),
    ) -> None:
        service = service_factory()
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
        except (InvalidRunIdError, RunNotFoundError) as exc:
            console.print(str(exc))
            raise typer.Exit(2) from exc
        except StateCorruptError as exc:
            console.print(str(exc))
            raise typer.Exit(3) from exc
        _print_record(console, record, artifact_dir)

    return app


app = create_app()


def main() -> None:
    app()


def _print_record(console: Console, record: object, artifact_dir: Path) -> None:
    status = getattr(record, "status")
    run_id = getattr(record, "run_id")
    console.print(f"{run_id}  {status}")
    failure_kind = getattr(record, "failure_kind")
    failure_detail = getattr(record, "failure_detail")
    if failure_kind is not None:
        console.print(f"failure: {failure_kind}: {failure_detail}")
    console.print(f"artifacts: {artifact_dir}", soft_wrap=True)


if __name__ == "__main__":
    main()
