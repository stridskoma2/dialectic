"""Bounded JSON-RPC ACP process lease with explicit capture epochs."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .launcher import DirectLaunchSpec, LaunchSpec, WindowsBatchLaunchSpec
from .output import strict_json_loads
from .process import (
    MAX_READER_CHUNK_BYTES,
    PosixProcessUnit,
    ProcessSupervisor,
    ReaderHandoffCoordinator,
    WindowsJobLauncher,
    WindowsReaderHandoff,
    join_reader_threads,
)
from .redaction import BoundedStreamCapture, CapturedStream, KnownCredentials
from .windows_process import CtypesWindowsJobBackend


class AcpError(RuntimeError):
    pass


class AcpProtocolError(AcpError):
    pass


class AcpTurnTimeout(AcpError):
    pass


@dataclass(frozen=True, slots=True)
class AcpLogicalResponse:
    session_id: str
    text: str
    actual_model: str | None
    usage: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class AcpEpochCapture:
    stdout: CapturedStream
    stderr: CapturedStream
    process_exit_code: int | None
    cleanup_confirmed: bool


class AcpLease(Protocol):
    process_unit_id: str
    session_id: str

    async def prompt(self, text: str, timeout_seconds: float) -> AcpLogicalResponse: ...

    async def switch_epoch(self) -> AcpEpochCapture: ...

    async def close(self, graceful_kill_seconds: float) -> AcpEpochCapture: ...


class AcpLeaseFactory(Protocol):
    async def open(
        self,
        plan: LaunchSpec,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        model: str,
        process_unit_id: str,
        stdout_limit: int,
        stderr_limit: int,
        credentials: KnownCredentials,
        preflight_seconds: float,
    ) -> AcpLease: ...


class ManagedAcpLeaseFactory:
    """Launch the ACP process in the release-platform process unit."""

    def __init__(self, *, windows_backend: object | None = None) -> None:
        self._windows_backend = windows_backend

    async def open(
        self,
        plan: LaunchSpec,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        model: str,
        process_unit_id: str,
        stdout_limit: int,
        stderr_limit: int,
        credentials: KnownCredentials,
        preflight_seconds: float,
    ) -> AcpLease:
        lease = _ManagedAcpLease(
            plan=plan,
            cwd=cwd,
            environment=environment,
            model=model,
            process_unit_id=process_unit_id,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            credentials=credentials,
            windows_backend=self._windows_backend,
        )
        await lease.start(preflight_seconds)
        return lease


class _ManagedAcpLease:
    def __init__(
        self,
        *,
        plan: LaunchSpec,
        cwd: Path,
        environment: Mapping[str, str],
        model: str,
        process_unit_id: str,
        stdout_limit: int,
        stderr_limit: int,
        credentials: KnownCredentials,
        windows_backend: object | None,
    ) -> None:
        self.plan = plan
        self.cwd = cwd
        self.environment = dict(environment)
        self.model = model
        self.process_unit_id = process_unit_id
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self.credentials = credentials
        self.windows_backend = windows_backend
        self.session_id = ""
        self._unit: Any = None
        self._stdin: Any = None
        self._stdout_capture = BoundedStreamCapture(stdout_limit, credentials)
        self._stderr_capture = BoundedStreamCapture(stderr_limit, credentials)
        self._capture_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_request_id = 1
        self._line_buffer = bytearray()
        self._turn_text: list[str] = []
        self._fatal: asyncio.Future[BaseException] | None = None
        self._wait_task: asyncio.Task[int] | None = None
        self._reader_tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._windows_backend_instance: Any = None
        self._windows_coordinator: ReaderHandoffCoordinator | None = None
        self._windows_handoffs: dict[str, WindowsReaderHandoff] = {}
        self._windows_readers: list[threading.Thread] = []
        self._windows_pump: asyncio.Task[None] | None = None
        self._windows_notification: asyncio.Event | None = None
        self._windows_io_finalized = False
        self._windows_io_cleanup_result = True
        self._activity_callback: Callable[[], None] | None = None

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Set the current logical turn's controller-owned liveness sink."""

        self._activity_callback = callback

    async def start(self, timeout_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        self._fatal = loop.create_future()
        try:
            if os.name == "nt":
                self._start_windows(loop)
            else:
                await self._start_posix()
            self._wait_task = asyncio.create_task(self._watch_exit())
            initialized = await self._request(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {}},
                timeout_seconds,
            )
            auth_methods = initialized.get("authMethods", [])
            if not isinstance(auth_methods, list):
                raise AcpProtocolError("ACP initialize authMethods is not a list")
            method_ids = {
                item.get("id")
                for item in auth_methods
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            method_id = (
                "xai.api_key"
                if "XAI_API_KEY" in self.environment and "xai.api_key" in method_ids
                else "cached_token" if "cached_token" in method_ids else None
            )
            if method_id is None:
                raise AcpProtocolError("ACP authentication method is unavailable")
            await self._request(
                "authenticate",
                {"methodId": method_id, "_meta": {"headless": True}},
                timeout_seconds,
            )
            created = await self._request(
                "session/new",
                {"cwd": str(self.cwd), "mcpServers": []},
                timeout_seconds,
            )
            session_id = created.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise AcpProtocolError("ACP session/new lacks a session id")
            self.session_id = session_id
        except BaseException:
            await self._abort_start()
            raise

    async def prompt(self, text: str, timeout_seconds: float) -> AcpLogicalResponse:
        if self._closed or not self.session_id:
            raise AcpProtocolError("ACP lease is not open")
        self._turn_text = []
        result = await self._request(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            timeout_seconds,
        )
        stop_reason = result.get("stopReason")
        if not isinstance(stop_reason, str) or not stop_reason:
            raise AcpProtocolError("ACP prompt response lacks a stop reason")
        actual_model = result.get("model")
        if actual_model is not None and not isinstance(actual_model, str):
            raise AcpProtocolError("ACP prompt model is invalid")
        usage = result.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise AcpProtocolError("ACP prompt usage is invalid")
        return AcpLogicalResponse(
            session_id=self.session_id,
            text="".join(self._turn_text),
            actual_model=actual_model,
            usage=usage,
        )

    async def switch_epoch(self) -> AcpEpochCapture:
        if self._closed:
            raise AcpProtocolError("ACP lease is closed")
        async with self._capture_lock:
            if os.name == "nt":
                try:
                    self._drain_windows_locked(switch=True)
                except RuntimeError as exc:
                    if self._fatal is not None and self._fatal.done():
                        raise self._fatal.result() from exc
                    raise AcpProtocolError(
                        "ACP reader epoch cannot be switched"
                    ) from exc
            if self._fatal is not None and self._fatal.done():
                raise self._fatal.result()
            previous = AcpEpochCapture(
                stdout=self._stdout_capture.finish(epoch_boundary=True),
                stderr=self._stderr_capture.finish(epoch_boundary=True),
                process_exit_code=None,
                cleanup_confirmed=True,
            )
            self._stdout_capture = BoundedStreamCapture(
                self.stdout_limit, self.credentials
            )
            self._stderr_capture = BoundedStreamCapture(
                self.stderr_limit, self.credentials
            )
            return previous

    async def close(self, graceful_kill_seconds: float) -> AcpEpochCapture:
        if self._closed:
            raise AcpProtocolError("ACP lease cleanup was requested more than once")
        self._closed = True
        await self._close_stdin()
        supervision = await ProcessSupervisor().supervise(
            self._unit,
            turn_timeout_seconds=min(0.1, graceful_kill_seconds),
            graceful_kill_seconds=graceful_kill_seconds,
        )
        readers_ok = (
            supervision.cleanup_confirmed
            if os.name == "nt"
            else await self._finish_readers(graceful_kill_seconds)
        )
        async with self._capture_lock:
            if os.name == "nt":
                self._drain_windows_locked(switch=False)
            stdout = self._stdout_capture.finish()
            stderr = self._stderr_capture.finish()
        return AcpEpochCapture(
            stdout=stdout,
            stderr=stderr,
            process_exit_code=supervision.exit_code,
            cleanup_confirmed=supervision.cleanup_confirmed and readers_ok,
        )

    async def _start_posix(self) -> None:
        if not isinstance(self.plan, DirectLaunchSpec):
            raise AcpProtocolError("Windows batch ACP launch is invalid on POSIX")
        self._unit = await PosixProcessUnit.launch(
            str(self.plan.executable),
            *self.plan.arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cwd),
            env=self.environment,
        )
        process = self._unit.process
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise AcpProtocolError("ACP standard streams are unavailable")
        self._stdin = process.stdin
        self._reader_tasks = [
            asyncio.create_task(self._read_posix(process.stdout, "stdout")),
            asyncio.create_task(self._read_posix(process.stderr, "stderr")),
        ]

    def _start_windows(self, loop: asyncio.AbstractEventLoop) -> None:
        backend = self.windows_backend or CtypesWindowsJobBackend()
        executable, arguments = _root_command(self.plan)
        unit = WindowsJobLauncher(backend).launch(
            executable=str(executable),
            arguments=arguments,
            cwd=str(self.cwd),
            environment=self.environment,
        )
        pipes = unit.pipes
        if set(pipes) != {"stdin", "stdout", "stderr"}:
            raise AcpProtocolError("ACP Win32 stream handle set is incomplete")
        self._unit = unit
        self._windows_backend_instance = backend
        self._stdin = pipes["stdin"][0]
        self._windows_coordinator = ReaderHandoffCoordinator()
        self._windows_notification = asyncio.Event()

        def notify() -> None:
            assert self._windows_notification is not None
            self._windows_notification.set()

        self._windows_handoffs = {
            "stdout": WindowsReaderHandoff(
                limit_bytes=self.stdout_limit,
                coordinator=self._windows_coordinator,
                notify=notify,
                loop=loop,
            ),
            "stderr": WindowsReaderHandoff(
                limit_bytes=self.stderr_limit,
                coordinator=self._windows_coordinator,
                notify=notify,
                loop=loop,
            ),
        }
        self._windows_readers = [
            threading.Thread(
                target=self._windows_handoffs[name].read_pipe,
                args=(_WindowsPipeReader(backend, pipes[name][0]),),
                daemon=True,
                name=f"dialectic-acp-{name}-reader",
            )
            for name in ("stdout", "stderr")
        ]
        for reader in self._windows_readers:
            reader.start()
        self._windows_pump = asyncio.create_task(self._pump_windows())

        async def cleanup_io(seconds: float) -> bool:
            return await self._finish_windows_io(seconds)

        unit.attach_io_cleanup(cleanup_io)

    async def _read_posix(
        self, source: asyncio.StreamReader, stream_name: str
    ) -> None:
        try:
            while chunk := await source.read(MAX_READER_CHUNK_BYTES):
                async with self._capture_lock:
                    self._accept_chunk(stream_name, chunk)
        except BaseException as exc:
            self._set_fatal(exc)

    async def _pump_windows(self) -> None:
        assert self._windows_notification is not None
        try:
            while not self._closed:
                await self._windows_notification.wait()
                self._windows_notification.clear()
                async with self._capture_lock:
                    self._drain_windows_locked(switch=False)
                if all(handoff.completed for handoff in self._windows_handoffs.values()):
                    return
        except BaseException as exc:
            self._set_fatal(exc)

    def _drain_windows_locked(self, *, switch: bool) -> None:
        coordinator = self._windows_coordinator
        if coordinator is None:
            return
        names = ("stdout", "stderr")
        handoffs = tuple(self._windows_handoffs[name] for name in names)
        if switch:
            batches = coordinator.switch_epoch(handoffs)
        else:
            batches = tuple(handoff.drain() for handoff in handoffs)
        for name, chunks in zip(names, batches, strict=True):
            for chunk in chunks:
                self._accept_chunk(name, chunk)
        if coordinator.overflow.is_set():
            for name, handoff in self._windows_handoffs.items():
                if handoff.epoch_accepted_bytes >= (
                    self.stdout_limit if name == "stdout" else self.stderr_limit
                ):
                    self._capture(name).mark_overflow()
            self._set_fatal(AcpProtocolError("ACP native stream exceeded its bound"))

    def _accept_chunk(self, stream_name: str, chunk: bytes) -> None:
        capture = self._capture(stream_name)
        before = capture.accepted_bytes
        overflow = capture.feed(chunk)
        accepted = chunk[: max(0, capture.accepted_bytes - before)]
        if stream_name == "stdout" and accepted:
            self._accept_protocol_bytes(accepted)
        if overflow:
            self._set_fatal(AcpProtocolError("ACP native stream exceeded its bound"))

    def _accept_protocol_bytes(self, chunk: bytes) -> None:
        self._line_buffer.extend(chunk)
        while True:
            newline = self._line_buffer.find(b"\n")
            if newline < 0:
                return
            line = bytes(self._line_buffer[:newline])
            del self._line_buffer[: newline + 1]
            if not line.strip():
                continue
            try:
                text = line.decode("utf-8", errors="strict")
                message = strict_json_loads(text)
                self._dispatch_message(message)
            except BaseException as exc:
                self._set_fatal(
                    exc if isinstance(exc, AcpError) else AcpProtocolError("invalid ACP JSONL")
                )

    def _dispatch_message(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise AcpProtocolError("ACP message is not JSON-RPC 2.0")
        if "method" in message:
            if message.get("method") != "session/update":
                raise AcpProtocolError("ACP agent sent an unsupported client request")
            params = message.get("params")
            if not isinstance(params, dict):
                raise AcpProtocolError("ACP session/update params are invalid")
            session_id = params.get("sessionId")
            if self.session_id and session_id != self.session_id:
                raise AcpProtocolError("ACP session/update mismatches the owned session")
            update = params.get("update")
            if not isinstance(update, dict) or not isinstance(
                update.get("sessionUpdate"), str
            ):
                raise AcpProtocolError("ACP session/update payload is invalid")
            if self._activity_callback is not None:
                self._activity_callback()
            if update["sessionUpdate"] == "agent_message_chunk":
                content = update.get("content")
                if not isinstance(content, dict) or not isinstance(
                    content.get("text"), str
                ):
                    raise AcpProtocolError("ACP message chunk lacks text")
                self._turn_text.append(content["text"])
            return
        request_id = message.get("id")
        if type(request_id) is not int or request_id not in self._pending:
            raise AcpProtocolError("ACP response id is out of sequence")
        future = self._pending.pop(request_id)
        if "error" in message:
            future.set_exception(AcpProtocolError("ACP request returned an error"))
            return
        result = message.get("result", {})
        if not isinstance(result, dict):
            future.set_exception(AcpProtocolError("ACP response result is not an object"))
            return
        future.set_result(result)

    async def _request(
        self, method: str, params: Mapping[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        if self._fatal is None:
            raise AcpProtocolError("ACP process is not initialized")
        if self._fatal.done():
            raise self._fatal.result()
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        await self._write(frame)
        fatal_wait = asyncio.ensure_future(asyncio.shield(self._fatal))
        try:
            done, _ = await asyncio.wait(
                {future, fatal_wait},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fatal_wait in done:
                raise fatal_wait.result()
            if future in done:
                return future.result()
            raise AcpTurnTimeout(f"ACP {method} reached its response deadline")
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            if not fatal_wait.done():
                fatal_wait.cancel()
            await asyncio.gather(fatal_wait, return_exceptions=True)

    async def _write(self, frame: bytes) -> None:
        async with self._write_lock:
            if os.name == "nt":
                await asyncio.to_thread(
                    self._windows_backend_instance.write_pipe, self._stdin, frame
                )
            else:
                self._stdin.write(frame)
                await self._stdin.drain()

    async def _close_stdin(self) -> None:
        if self._stdin is None:
            return
        if os.name == "nt":
            backend = self._windows_backend_instance
            if not backend.resource_is_closed(self._stdin):
                backend.close_resource(self._stdin)
        else:
            self._stdin.close()
            try:
                await self._stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _watch_exit(self) -> int:
        try:
            code = await self._unit.wait()
            if not self._closed:
                self._set_fatal(
                    AcpProtocolError("ACP process exited before finalization")
                )
            return code
        except BaseException as exc:
            self._set_fatal(exc)
            raise

    async def _finish_readers(self, timeout_seconds: float) -> bool:
        if os.name == "nt":
            return await self._finish_windows_io(timeout_seconds)
        if not self._reader_tasks:
            return True
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*self._reader_tasks, return_exceptions=True),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            for task in self._reader_tasks:
                task.cancel()
            await asyncio.gather(*self._reader_tasks, return_exceptions=True)
            return False
        return not any(isinstance(result, BaseException) for result in results)

    async def _finish_windows_io(self, timeout_seconds: float) -> bool:
        if self._windows_io_finalized:
            return self._windows_io_cleanup_result
        coordinator = self._windows_coordinator
        if coordinator is None:
            return True
        coordinator.trigger_abort()
        joined = await join_reader_threads(
            self._windows_readers,
            coordinator=coordinator,
            timeout_seconds=timeout_seconds,
            abort_first=False,
        )
        if self._windows_pump is not None and not self._windows_pump.done():
            self._windows_pump.cancel()
            await asyncio.gather(self._windows_pump, return_exceptions=True)
        async with self._capture_lock:
            self._drain_windows_locked(switch=False)
        try:
            for handoff in self._windows_handoffs.values():
                handoff.raise_reader_error()
        except BaseException:
            joined = False
        self._windows_io_finalized = True
        self._windows_io_cleanup_result = joined
        return joined

    async def _abort_start(self) -> None:
        if self._unit is None:
            return
        self._closed = True
        try:
            await self._close_stdin()
            await self._unit.force_terminate()
            await self._unit.confirm_cleanup(1.0)
            if os.name != "nt":
                await self._finish_readers(1.0)
        except BaseException:
            pass

    def _capture(self, stream_name: str) -> BoundedStreamCapture:
        return self._stdout_capture if stream_name == "stdout" else self._stderr_capture

    def _set_fatal(self, error: BaseException) -> None:
        if self._fatal is not None and not self._fatal.done():
            self._fatal.set_result(error)


class _WindowsPipeReader:
    def __init__(self, backend: object, handle: object) -> None:
        self._backend = backend
        self._handle = handle

    def read(self, maximum_bytes: int) -> bytes:
        return self._backend.read_pipe(self._handle, maximum_bytes)


def _root_command(plan: LaunchSpec) -> tuple[Path, tuple[str, ...]]:
    if isinstance(plan, DirectLaunchSpec):
        return plan.executable, plan.arguments
    if isinstance(plan, WindowsBatchLaunchSpec):
        return plan.spawned_root_executable, plan.root_arguments
    raise TypeError("unknown launch plan")
