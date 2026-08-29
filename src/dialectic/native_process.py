"""Bounded native process transport shared by versioned agent adapters."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .launcher import DirectLaunchSpec, LaunchSpec, WindowsBatchLaunchSpec
from .process import (
    MAX_READER_CHUNK_BYTES,
    PosixProcessUnit,
    ProcessSupervisor,
    ReaderHandoffCoordinator,
    SupervisionResult,
    WindowsJobLauncher,
    WindowsReaderHandoff,
    join_reader_threads,
)
from .redaction import BoundedStreamCapture, CapturedStream, KnownCredentials
from .windows_process import CtypesWindowsJobBackend


class NativeLaunchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NativeProcessResult:
    process_started: bool
    exit_code: int | None
    end_reason: str
    failure_kind: str | None
    cleanup_confirmed: bool
    stdout: CapturedStream
    stderr: CapturedStream


class NativeProcessTransport(Protocol):
    async def run(
        self,
        plan: LaunchSpec,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes,
        stdout_limit: int,
        stderr_limit: int,
        timeout_seconds: float,
        graceful_kill_seconds: float,
        credentials: KnownCredentials,
        cancellation: asyncio.Event | None = None,
    ) -> NativeProcessResult: ...


class BoundedNativeProcessTransport:
    def __init__(self, *, windows_backend: object | None = None) -> None:
        self._windows_backend = windows_backend

    async def run(
        self,
        plan: LaunchSpec,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdin: bytes,
        stdout_limit: int,
        stderr_limit: int,
        timeout_seconds: float,
        graceful_kill_seconds: float,
        credentials: KnownCredentials,
        cancellation: asyncio.Event | None = None,
    ) -> NativeProcessResult:
        if os.name == "nt":
            return await self._run_windows(
                plan, cwd=cwd, environment=environment, stdin=stdin,
                stdout_limit=stdout_limit, stderr_limit=stderr_limit,
                timeout_seconds=timeout_seconds,
                graceful_kill_seconds=graceful_kill_seconds,
                credentials=credentials, cancellation=cancellation,
            )
        return await self._run_posix(
            plan, cwd=cwd, environment=environment, stdin=stdin,
            stdout_limit=stdout_limit, stderr_limit=stderr_limit,
            timeout_seconds=timeout_seconds,
            graceful_kill_seconds=graceful_kill_seconds,
            credentials=credentials, cancellation=cancellation,
        )

    async def _run_posix(
        self, plan: LaunchSpec, *, cwd: Path, environment: Mapping[str, str],
        stdin: bytes, stdout_limit: int, stderr_limit: int,
        timeout_seconds: float, graceful_kill_seconds: float,
        credentials: KnownCredentials, cancellation: asyncio.Event | None,
    ) -> NativeProcessResult:
        if not isinstance(plan, DirectLaunchSpec):
            raise NativeLaunchError("Windows batch plans cannot run on POSIX")
        try:
            unit = await PosixProcessUnit.launch(
                str(plan.executable), *plan.arguments,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=dict(environment),
            )
        except Exception as exc:
            raise NativeLaunchError("native process launch failed") from exc
        process = unit.process
        assert process.stdin and process.stdout and process.stderr
        stdout_capture = BoundedStreamCapture(stdout_limit, credentials)
        stderr_capture = BoundedStreamCapture(stderr_limit, credentials)
        overflow = asyncio.Event()

        async def writer() -> None:
            try:
                process.stdin.write(stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        async def reader(
            source: asyncio.StreamReader, capture: BoundedStreamCapture
        ) -> None:
            while chunk := await source.read(MAX_READER_CHUNK_BYTES):
                if capture.feed(chunk):
                    overflow.set()
                    return

        tasks = [
            asyncio.create_task(writer()),
            asyncio.create_task(reader(process.stdout, stdout_capture)),
            asyncio.create_task(reader(process.stderr, stderr_capture)),
        ]
        supervision = await _supervise_fail_closed(
            unit,
            timeout_seconds=timeout_seconds,
            graceful_kill_seconds=graceful_kill_seconds,
            cancellation=cancellation,
            overflow=overflow,
        )
        if not await _settle_tasks(tasks, graceful_kill_seconds):
            supervision = SupervisionResult(
                supervision.exit_code, "cleanup-failed",
                "PROCESS_CLEANUP_FAILED", False,
            )
        return _result(supervision, stdout_capture.finish(), stderr_capture.finish())

    async def _run_windows(
        self, plan: LaunchSpec, *, cwd: Path, environment: Mapping[str, str],
        stdin: bytes, stdout_limit: int, stderr_limit: int,
        timeout_seconds: float, graceful_kill_seconds: float,
        credentials: KnownCredentials, cancellation: asyncio.Event | None,
    ) -> NativeProcessResult:
        backend = self._windows_backend or CtypesWindowsJobBackend()
        executable, arguments = _root_command(plan)
        try:
            unit = WindowsJobLauncher(backend).launch(
                executable=str(executable), arguments=arguments,
                cwd=str(cwd), environment=environment,
            )
        except Exception as exc:
            raise NativeLaunchError("native process launch failed") from exc
        pipes = unit.pipes
        if set(pipes) != {"stdin", "stdout", "stderr"}:
            await unit.force_terminate()
            await unit.confirm_cleanup(graceful_kill_seconds)
            raise NativeLaunchError("Win32 standard-stream handle set is incomplete")

        loop = asyncio.get_running_loop()
        notification = asyncio.Event()
        overflow = asyncio.Event()
        coordinator = ReaderHandoffCoordinator()

        def notify() -> None:
            notification.set()
            if coordinator.overflow.is_set():
                overflow.set()

        handoffs = {
            "stdout": WindowsReaderHandoff(
                limit_bytes=stdout_limit, coordinator=coordinator,
                notify=notify, loop=loop,
            ),
            "stderr": WindowsReaderHandoff(
                limit_bytes=stderr_limit, coordinator=coordinator,
                notify=notify, loop=loop,
            ),
        }
        captures = {
            "stdout": BoundedStreamCapture(stdout_limit, credentials),
            "stderr": BoundedStreamCapture(stderr_limit, credentials),
        }
        readers = [
            threading.Thread(
                target=handoffs[name].read_pipe,
                args=(_WindowsPipeReader(backend, pipes[name][0]),),
                daemon=True, name=f"dialectic-{name}-reader",
            )
            for name in ("stdout", "stderr")
        ]
        writer_errors: list[BaseException] = []

        def write_input() -> None:
            parent = pipes["stdin"][0]
            try:
                backend.write_pipe(parent, stdin)
            except BaseException as exc:
                if getattr(exc, "winerror", None) != 109:
                    writer_errors.append(exc)
            finally:
                if not backend.resource_is_closed(parent):
                    backend.close_resource(parent)

        writer = threading.Thread(
            target=write_input, daemon=True, name="dialectic-stdin-writer"
        )
        for thread in readers:
            thread.start()
        writer.start()

        def drain() -> None:
            for name, handoff in handoffs.items():
                for chunk in handoff.drain():
                    if captures[name].feed(chunk):
                        overflow.set()

        async def cleanup_io(seconds: float) -> bool:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and any(t.is_alive() for t in readers):
                drain()
                notification.clear()
                try:
                    await asyncio.wait_for(
                        notification.wait(),
                        timeout=min(0.05, max(0.0, deadline - time.monotonic())),
                    )
                except TimeoutError:
                    pass
            drain()
            if any(t.is_alive() for t in readers):
                coordinator.trigger_abort()
            joined = await join_reader_threads(
                readers, coordinator=coordinator,
                timeout_seconds=max(0.0, deadline - time.monotonic()),
                abort_first=False,
            )
            await asyncio.to_thread(writer.join, max(0.0, deadline - time.monotonic()))
            drain()
            try:
                for handoff in handoffs.values():
                    handoff.raise_reader_error()
            except BaseException:
                return False
            return joined and not writer.is_alive() and not writer_errors

        unit.attach_io_cleanup(cleanup_io)
        supervision = await _supervise_fail_closed(
            unit,
            timeout_seconds=timeout_seconds,
            graceful_kill_seconds=graceful_kill_seconds,
            cancellation=cancellation,
            overflow=overflow,
        )
        drain()
        return _result(
            supervision, captures["stdout"].finish(), captures["stderr"].finish()
        )


class _WindowsPipeReader:
    def __init__(self, backend: object, handle: object) -> None:
        self._backend = backend
        self._handle = handle

    def read(self, maximum_bytes: int) -> bytes:
        return self._backend.read_pipe(self._handle, maximum_bytes)


async def _settle_tasks(tasks: list[asyncio.Task[None]], timeout: float) -> bool:
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return False
    return not any(isinstance(result, BaseException) for result in results)


async def _supervise_fail_closed(
    unit: object,
    *,
    timeout_seconds: float,
    graceful_kill_seconds: float,
    cancellation: asyncio.Event | None,
    overflow: asyncio.Event,
) -> SupervisionResult:
    try:
        return await ProcessSupervisor().supervise(
            unit,  # type: ignore[arg-type]
            turn_timeout_seconds=timeout_seconds,
            graceful_kill_seconds=graceful_kill_seconds,
            cancellation=cancellation,
            overflow=overflow,
        )
    except BaseException:
        try:
            await unit.force_terminate()  # type: ignore[attr-defined]
            await unit.confirm_cleanup(graceful_kill_seconds)  # type: ignore[attr-defined]
        except BaseException:
            pass
        return SupervisionResult(
            None,
            "cleanup-failed",
            "PROCESS_CLEANUP_FAILED",
            False,
        )


def _root_command(plan: LaunchSpec) -> tuple[Path, tuple[str, ...]]:
    if isinstance(plan, DirectLaunchSpec):
        return plan.executable, plan.arguments
    if isinstance(plan, WindowsBatchLaunchSpec):
        return plan.spawned_root_executable, plan.root_arguments
    raise TypeError("unknown launch plan")


def _result(
    supervision: SupervisionResult,
    stdout: CapturedStream,
    stderr: CapturedStream,
) -> NativeProcessResult:
    return NativeProcessResult(
        process_started=True,
        exit_code=supervision.exit_code,
        end_reason=supervision.termination_reason,
        failure_kind=supervision.failure_kind,
        cleanup_confirmed=supervision.cleanup_confirmed,
        stdout=stdout,
        stderr=stderr,
    )
