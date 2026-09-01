"""Platform process-unit supervision and bounded Windows reader handoff."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, BinaryIO, Callable, Literal, Mapping, Protocol, Sequence

from .contracts import FailureKind
from .launcher import DirectLaunchSpec, LaunchSpec, WindowsBatchLaunchSpec

MAX_READER_CHUNK_BYTES = 65_536
EXTENDED_STARTUPINFO_PRESENT = 0x0008_0000
CREATE_SUSPENDED = 0x0000_0004
CREATE_UNICODE_ENVIRONMENT = 0x0000_0400


class ProcessUnit(Protocol):
    async def wait(self) -> int: ...

    async def request_graceful_termination(self) -> bool | None: ...

    async def force_terminate(self) -> None: ...

    async def confirm_cleanup(self, timeout_seconds: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class SupervisionResult:
    exit_code: int | None
    termination_reason: Literal[
        "completed", "timeout", "cancelled", "output-limit", "cleanup-failed"
    ]
    failure_kind: FailureKind | None
    cleanup_confirmed: bool


class ProcessSupervisor:
    async def supervise(
        self,
        unit: ProcessUnit,
        *,
        turn_timeout_seconds: float,
        graceful_kill_seconds: float,
        cancellation: asyncio.Event | None = None,
        overflow: asyncio.Event | None = None,
    ) -> SupervisionResult:
        root_wait = asyncio.create_task(unit.wait())
        cancel_wait = asyncio.create_task((cancellation or asyncio.Event()).wait())
        overflow_wait = asyncio.create_task((overflow or asyncio.Event()).wait())
        try:
            done, _ = await asyncio.wait(
                {root_wait, cancel_wait, overflow_wait},
                timeout=turn_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if root_wait in done:
                exit_code = root_wait.result()
                reason: Literal["completed", "timeout", "cancelled", "output-limit"] = "completed"
            elif cancel_wait in done:
                exit_code = None
                reason = "cancelled"
            elif overflow_wait in done:
                exit_code = None
                reason = "output-limit"
            else:
                exit_code = None
                reason = "timeout"

            if reason != "completed":
                graceful_delivered = await unit.request_graceful_termination()
                if graceful_delivered is False:
                    await unit.force_terminate()
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(root_wait), timeout=graceful_kill_seconds
                        )
                    except TimeoutError:
                        await unit.force_terminate()
            else:
                # A normal root exit still owns and reaps lingering unit members.
                await unit.force_terminate()

            confirmed = await unit.confirm_cleanup(graceful_kill_seconds)
            if exit_code is None and root_wait.done() and not root_wait.cancelled():
                try:
                    exit_code = root_wait.result()
                except Exception:
                    confirmed = False
            if not confirmed:
                return SupervisionResult(
                    exit_code,
                    "cleanup-failed",
                    "PROCESS_CLEANUP_FAILED",
                    False,
                )
            failure: FailureKind | None = "AGENT_OUTPUT_TOO_LARGE" if reason == "output-limit" else None
            return SupervisionResult(exit_code, reason, failure, True)
        finally:
            for task in (root_wait, cancel_wait, overflow_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(root_wait, cancel_wait, overflow_wait, return_exceptions=True)

    async def supervise_many(
        self,
        units: list[ProcessUnit],
        *,
        turn_timeout_seconds: float,
        graceful_kill_seconds: float,
        cancellation: asyncio.Event,
    ) -> list[SupervisionResult]:
        return await asyncio.gather(
            *(
                self.supervise(
                    unit,
                    turn_timeout_seconds=turn_timeout_seconds,
                    graceful_kill_seconds=graceful_kill_seconds,
                    cancellation=cancellation,
                )
                for unit in units
            )
        )


class PosixProcessUnit:
    """POSIX session/process-group ownership; not a daemonization boundary."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            raise OSError("PosixProcessUnit is not available on Windows")
        self.process = process
        self.process_group = process.pid

    @classmethod
    async def launch(cls, executable: str, *arguments: str, **kwargs: object) -> "PosixProcessUnit":
        if os.name == "nt":
            raise OSError("POSIX process launch is not available on Windows")
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            start_new_session=True,
            **kwargs,
        )
        return cls(process)

    async def wait(self) -> int:
        return await self.process.wait()

    async def request_graceful_termination(self) -> bool:
        return self._signal_group(signal.SIGTERM)

    async def force_terminate(self) -> None:
        self._signal_group(signal.SIGKILL)

    async def confirm_cleanup(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        try:
            await asyncio.wait_for(
                self.process.wait(), timeout=max(0.0, deadline - time.monotonic())
            )
        except TimeoutError:
            return False
        while time.monotonic() <= deadline:
            try:
                os.killpg(self.process_group, 0)
            except ProcessLookupError:
                return True
            await asyncio.sleep(0.01)
        return False

    def _signal_group(self, sig: signal.Signals) -> bool:
        try:
            os.killpg(self.process_group, sig)
        except ProcessLookupError:
            return False
        return True


class FakeProcessUnit:
    """Deterministic process tree used by Slice 0 supervision tests."""

    def __init__(
        self,
        *,
        root_delay: float,
        exit_code: int = 0,
        cleanup_confirmed: bool = True,
        sentinel_delay: float | None = None,
        sentinel: Callable[[], None] | None = None,
    ) -> None:
        self.root_delay = root_delay
        self.exit_code = exit_code
        self.cleanup_will_confirm = cleanup_confirmed
        self.sentinel_delay = sentinel_delay
        self.sentinel = sentinel
        self._root_task: asyncio.Task[int] | None = None
        self._descendant: asyncio.Task[None] | None = None
        self.graceful_requested = False
        self.forced = False

    async def wait(self) -> int:
        if self._root_task is None:
            self._root_task = asyncio.create_task(self._run_root())
            if self.sentinel_delay is not None:
                self._descendant = asyncio.create_task(self._run_descendant())
        return await self._root_task

    async def _run_root(self) -> int:
        await asyncio.sleep(self.root_delay)
        return self.exit_code

    async def _run_descendant(self) -> None:
        await asyncio.sleep(self.sentinel_delay or 0)
        if self.sentinel is not None:
            self.sentinel()

    async def request_graceful_termination(self) -> bool:
        self.graceful_requested = True
        return True

    async def force_terminate(self) -> None:
        self.forced = True
        for task in (self._root_task, self._descendant):
            if task is not None and not task.done():
                task.cancel()

    async def confirm_cleanup(self, timeout_seconds: float) -> bool:
        tasks = [task for task in (self._root_task, self._descendant) if task is not None]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_seconds
                )
            except TimeoutError:
                return False
        return self.cleanup_will_confirm


@dataclass(frozen=True, slots=True)
class WindowsCreatedProcess:
    process_handle: object
    thread_handle: object
    process_id: int


class WindowsJobBackend(Protocol):
    """Narrow injectable boundary around the release-platform Win32 calls."""

    def nested_jobs_supported(self) -> bool: ...

    def create_kill_on_close_job(self) -> object: ...

    def create_standard_stream_pipes(self) -> Mapping[str, tuple[object, object]]: ...

    def create_attribute_list(
        self, *, job: object, inherited_handles: Sequence[object]
    ) -> object: ...

    def create_process_suspended(
        self,
        *,
        executable: str,
        arguments: Sequence[str],
        cwd: str,
        environment: Mapping[str, str],
        attribute_list: object,
        creation_flags: int,
    ) -> WindowsCreatedProcess: ...

    def verify_job_membership(self, process: object, job: object) -> bool: ...

    def resume_thread(self, thread: object) -> None: ...

    def request_graceful_termination(self, process: object) -> bool | None: ...

    def terminate_job(self, job: object) -> None: ...

    def wait_process(self, process: object, timeout_seconds: float | None) -> int | None: ...

    def close_resource(self, resource: object) -> None: ...

    def resource_is_closed(self, resource: object) -> bool: ...


class WindowsJobLauncher:
    """Enforce creation-time Job assignment before the target entry point runs."""

    def __init__(self, backend: WindowsJobBackend) -> None:
        self._backend = backend

    def launch(
        self,
        *,
        executable: str,
        arguments: Sequence[str],
        cwd: str,
        environment: Mapping[str, str],
    ) -> "WindowsJobProcessUnit":
        if not self._backend.nested_jobs_supported():
            raise RuntimeError("nested Windows Job execution is unavailable")
        job: object | None = None
        pipes: Mapping[str, tuple[object, object]] = {}
        attribute_list: object | None = None
        created: WindowsCreatedProcess | None = None
        closed_resource_ids: set[int] = set()
        try:
            job = self._backend.create_kill_on_close_job()
            pipes = self._backend.create_standard_stream_pipes()
            child_handles = tuple(pair[1] for pair in pipes.values())
            attribute_list = self._backend.create_attribute_list(
                job=job,
                inherited_handles=child_handles,
            )
            created = self._backend.create_process_suspended(
                executable=executable,
                arguments=tuple(arguments),
                cwd=cwd,
                environment=environment,
                attribute_list=attribute_list,
                creation_flags=(
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
                ),
            )
            if not self._backend.verify_job_membership(created.process_handle, job):
                raise RuntimeError("creation-time Windows Job membership could not be verified")
            self._backend.resume_thread(created.thread_handle)
            child_handles = tuple(pair[1] for pair in pipes.values())
            for child_handle in child_handles:
                self._backend.close_resource(child_handle)
                closed_resource_ids.add(id(child_handle))
            return WindowsJobProcessUnit(
                self._backend,
                job=job,
                process=created,
                pipes=pipes,
                attribute_list=attribute_list,
                preclosed_resources=child_handles,
            )
        except Exception:
            if job is not None:
                try:
                    self._backend.terminate_job(job)
                except Exception:
                    pass
            resources: list[object] = []
            if created is not None:
                resources.extend((created.thread_handle, created.process_handle))
            resources.extend(handle for pair in pipes.values() for handle in pair)
            if attribute_list is not None:
                resources.append(attribute_list)
            if job is not None:
                resources.append(job)
            _close_distinct_resources(
                self._backend,
                resources,
                skip_ids=closed_resource_ids,
            )
            raise


class WindowsJobProcessUnit:
    def __init__(
        self,
        backend: WindowsJobBackend,
        *,
        job: object,
        process: WindowsCreatedProcess,
        pipes: Mapping[str, tuple[object, object]],
        attribute_list: object,
        preclosed_resources: Sequence[object],
    ) -> None:
        self._backend = backend
        self._job = job
        self._process = process
        self._pipes = dict(pipes)
        self._all_resources = [
            process.thread_handle,
            process.process_handle,
            *(handle for pair in pipes.values() for handle in pair),
            attribute_list,
            job,
        ]
        preclosed_ids = {id(resource) for resource in preclosed_resources}
        self._open_resources = [
            resource for resource in self._all_resources if id(resource) not in preclosed_ids
        ]
        self._closed = False
        self._io_cleanup: Callable[[float], Awaitable[bool]] | None = None

    @property
    def backend(self) -> WindowsJobBackend:
        return self._backend

    @property
    def pipes(self) -> Mapping[str, tuple[object, object]]:
        return self._pipes

    def attach_io_cleanup(self, cleanup: Callable[[float], Awaitable[bool]]) -> None:
        if self._io_cleanup is not None:
            raise RuntimeError("Windows process-unit I/O cleanup is already attached")
        self._io_cleanup = cleanup

    async def wait(self) -> int:
        result = await asyncio.to_thread(
            self._backend.wait_process, self._process.process_handle, None
        )
        if result is None:
            raise RuntimeError("unbounded process wait returned without an exit code")
        return result

    async def request_graceful_termination(self) -> bool | None:
        return await asyncio.to_thread(
            self._backend.request_graceful_termination, self._process.process_handle
        )

    async def force_terminate(self) -> None:
        await asyncio.to_thread(self._backend.terminate_job, self._job)

    async def confirm_cleanup(self, timeout_seconds: float) -> bool:
        if self._closed:
            return all(self._backend.resource_is_closed(resource) for resource in self._all_resources)
        deadline = time.monotonic() + timeout_seconds
        exit_code = await asyncio.to_thread(
            self._backend.wait_process,
            self._process.process_handle,
            max(0.0, deadline - time.monotonic()),
        )
        io_confirmed = True
        if self._io_cleanup is not None:
            io_confirmed = await self._io_cleanup(
                max(0.0, deadline - time.monotonic())
            )
        _close_distinct_resources(self._backend, self._open_resources)
        self._closed = True
        return exit_code is not None and io_confirmed and all(
            self._backend.resource_is_closed(resource) for resource in self._all_resources
        )


def _close_distinct_resources(
    backend: WindowsJobBackend,
    resources: Sequence[object],
    *,
    skip_ids: set[int] | None = None,
) -> None:
    seen: set[int] = set(skip_ids or ())
    for resource in resources:
        identity = id(resource)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if not backend.resource_is_closed(resource):
                backend.close_resource(resource)
        except Exception:
            # Closure proof below converts any remaining resource into cleanup failure.
            pass


class WindowsPipeReader:
    """Binary reader adapter around one backend-owned Windows pipe handle."""

    def __init__(self, backend: object, handle: object) -> None:
        self._backend = backend
        self._handle = handle

    def read(self, maximum_bytes: int) -> bytes:
        return self._backend.read_pipe(self._handle, maximum_bytes)


def root_command(plan: LaunchSpec) -> tuple[Path, tuple[str, ...]]:
    """Resolve the OS root executable and arguments for one launch plan."""

    if isinstance(plan, DirectLaunchSpec):
        return plan.executable, plan.arguments
    if isinstance(plan, WindowsBatchLaunchSpec):
        return plan.spawned_root_executable, plan.root_arguments
    raise TypeError("unknown launch plan")


class ReaderHandoffCoordinator:
    """Shared one-shot overflow/abort state for both Windows pipe readers."""

    def __init__(self) -> None:
        self.abort = threading.Event()
        self.overflow = threading.Event()
        self._lock = threading.Lock()
        self._admission_lock = threading.RLock()
        self._conditions: list[threading.Condition] = []
        self._overflow_transition_count = 0

    @property
    def overflow_transition_count(self) -> int:
        return self._overflow_transition_count

    def register(self, condition: threading.Condition) -> None:
        with self._lock:
            self._conditions.append(condition)

    def trigger_overflow(self) -> bool:
        with self._lock:
            first = not self.overflow.is_set()
            if first:
                self.overflow.set()
                self._overflow_transition_count += 1
            conditions = tuple(self._conditions)
        for condition in conditions:
            with condition:
                condition.notify_all()
        return first

    def trigger_abort(self) -> None:
        self.abort.set()
        with self._lock:
            conditions = tuple(self._conditions)
        for condition in conditions:
            with condition:
                condition.notify_all()

    def switch_epoch(
        self, handoffs: Sequence["WindowsReaderHandoff"]
    ) -> tuple[list[bytes], ...]:
        """Drain and reset a cohort at one reader-admission boundary."""

        with self._admission_lock:
            if self.abort.is_set() or self.overflow.is_set():
                raise RuntimeError("cannot switch a terminated reader epoch")
            return tuple(handoff._switch_epoch_locked() for handoff in handoffs)


class WindowsReaderHandoff:
    """A byte-bounded blocking-reader queue with bounded loop notifications."""

    def __init__(
        self,
        *,
        limit_bytes: int,
        coordinator: ReaderHandoffCoordinator,
        notify: Callable[[], None],
        loop: asyncio.AbstractEventLoop | None = None,
        queue_capacity_bytes: int | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> None:
        capacity = (
            min(limit_bytes, MAX_READER_CHUNK_BYTES)
            if queue_capacity_bytes is None
            else queue_capacity_bytes
        )
        if limit_bytes <= 0 or not 0 < capacity <= limit_bytes:
            raise ValueError("reader handoff capacity must be within the stream limit")
        self.limit_bytes = limit_bytes
        self.queue_capacity_bytes = capacity
        self.coordinator = coordinator
        self._notify = notify
        self._loop = loop
        self._on_activity = on_activity
        self._condition = threading.Condition()
        self.coordinator.register(self._condition)
        self._queue: deque[bytes] = deque()
        self._queued_bytes = 0
        self._accepted_bytes = 0
        self._data_notification_pending = False
        self._terminal_notification_sent = False
        self._completed = False
        self._reader_error: BaseException | None = None
        self.notification_count = 0
        self.peak_queued_bytes = 0
        self.peak_resident_bytes = 0

    @property
    def queued_bytes(self) -> int:
        with self._condition:
            return self._queued_bytes

    @property
    def completed(self) -> bool:
        with self._condition:
            return self._completed

    @property
    def epoch_accepted_bytes(self) -> int:
        with self._condition:
            return self._accepted_bytes

    def switch_epoch(self) -> list[bytes]:
        """Atomically close one admission epoch without restarting the reader."""

        return self.coordinator.switch_epoch((self,))[0]

    def _switch_epoch_locked(self) -> list[bytes]:
        with self._condition:
            chunks = list(self._queue)
            self._queue.clear()
            self._queued_bytes = 0
            self._accepted_bytes = 0
            self._data_notification_pending = False
            self._condition.notify_all()
            return chunks

    def read_pipe(self, pipe: BinaryIO) -> None:
        current_chunk_bytes = 0
        try:
            while not self.coordinator.abort.is_set() and not self.coordinator.overflow.is_set():
                chunk = pipe.read(MAX_READER_CHUNK_BYTES)
                if not chunk:
                    break
                if self._on_activity is not None:
                    self._on_activity()
                current_chunk_bytes = len(chunk)
                if current_chunk_bytes > MAX_READER_CHUNK_BYTES:
                    raise RuntimeError("reader returned a chunk larger than 65536 bytes")
                remaining_total = self.limit_bytes - self._accepted_bytes
                accepted_chunk = chunk[:remaining_total]
                accepted_length = len(accepted_chunk)
                overflow_now = current_chunk_bytes > remaining_total
                admitted = False
                while not admitted:
                    with self._condition:
                        self.peak_resident_bytes = max(
                            self.peak_resident_bytes,
                            self._queued_bytes + current_chunk_bytes,
                        )
                        while (
                            not overflow_now
                            and self._queued_bytes + accepted_length
                            > self.queue_capacity_bytes
                            and not self.coordinator.abort.is_set()
                            and not self.coordinator.overflow.is_set()
                        ):
                            self._condition.wait()
                    with self.coordinator._admission_lock:
                        with self._condition:
                            if (
                                self.coordinator.abort.is_set()
                                or self.coordinator.overflow.is_set()
                            ):
                                admitted = True
                                break
                            if (
                                not overflow_now
                                and self._queued_bytes + accepted_length
                                > self.queue_capacity_bytes
                            ):
                                continue
                            if accepted_chunk:
                                self._enqueue(accepted_chunk)
                            admitted = True
                if self.coordinator.abort.is_set() or (
                    self.coordinator.overflow.is_set() and not overflow_now
                ):
                    break
                if overflow_now:
                    self.coordinator.trigger_overflow()
                    break
                current_chunk_bytes = 0
        except BaseException as exc:
            self._reader_error = exc
        finally:
            with self._condition:
                self._completed = True
                if not self._terminal_notification_sent:
                    self._terminal_notification_sent = True
                    self.notification_count += 1
                    self._schedule_notification()
                self._condition.notify_all()

    def drain(self) -> list[bytes]:
        with self._condition:
            chunks = list(self._queue)
            self._queue.clear()
            self._queued_bytes = 0
            self._data_notification_pending = False
            self._condition.notify_all()
            return chunks

    def _enqueue(self, chunk: bytes) -> None:
        was_empty = not self._queue
        self._queue.append(chunk)
        self._queued_bytes += len(chunk)
        self._accepted_bytes += len(chunk)
        self.peak_queued_bytes = max(self.peak_queued_bytes, self._queued_bytes)
        if was_empty and not self._data_notification_pending:
            self._data_notification_pending = True
            self.notification_count += 1
            self._schedule_notification()

    def _schedule_notification(self) -> None:
        if self._loop is None:
            self._notify()
        else:
            self._loop.call_soon_threadsafe(self._notify)

    def raise_reader_error(self) -> None:
        if self._reader_error is not None:
            raise self._reader_error


async def join_reader_threads(
    threads: list[threading.Thread],
    *,
    coordinator: ReaderHandoffCoordinator,
    timeout_seconds: float,
    abort_first: bool = True,
) -> bool:
    if abort_first:
        coordinator.trigger_abort()
    deadline = time.monotonic() + timeout_seconds

    async def join_one(thread: threading.Thread) -> bool:
        remaining = max(0.0, deadline - time.monotonic())
        await asyncio.to_thread(thread.join, remaining)
        return not thread.is_alive()

    results = await asyncio.gather(*(join_one(thread) for thread in threads))
    return all(results)
