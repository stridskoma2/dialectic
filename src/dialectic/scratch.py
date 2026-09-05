"""Iterative no-follow accounting and bounded reserved-tree cleanup."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from .task_cleanup import cancel_and_wait

_T = TypeVar("_T")


class ScratchError(RuntimeError):
    pass


class ScratchLimitExceeded(ScratchError):
    pass


class ScratchContainmentError(ScratchError):
    pass


class ScratchCleanupTimeout(ScratchError):
    pass


@dataclass(frozen=True, slots=True)
class ScratchLimits:
    max_bytes: int
    max_entries: int
    max_depth: int

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_entries, self.max_depth) <= 0:
            raise ValueError("scratch limits must be positive")


@dataclass(frozen=True, slots=True)
class ScratchUsage:
    logical_regular_file_bytes: int
    entry_count: int
    maximum_depth: int
    overage: str | None
    invalid_type: str | None

    @property
    def within_limits(self) -> bool:
        return self.overage is None and self.invalid_type is None


def scan_scratch(root: Path | str, limits: ScratchLimits) -> ScratchUsage:
    root_path = Path(root)
    try:
        root_info = root_path.lstat()
    except OSError as exc:
        raise ScratchContainmentError("scratch root cannot be inspected safely") from exc
    if not stat.S_ISDIR(root_info.st_mode) or _is_reparse(root_info):
        raise ScratchContainmentError("scratch root is not a non-reparse directory")
    if os.name != "nt":
        return _scan_scratch_posix(
            root_path,
            limits,
            root_identity=(root_info.st_dev, root_info.st_ino),
        )

    byte_count = 0
    entry_count = 0
    maximum_depth = 0
    invalid_type: str | None = None
    stack: list[tuple[Path, int]] = [(root_path, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            raise ScratchContainmentError("scratch traversal could not open a directory") from exc
        with iterator:
            for entry in iterator:
                child_depth = depth + 1
                entry_count += 1
                if entry_count > limits.max_entries:
                    return ScratchUsage(
                        byte_count,
                        limits.max_entries + 1,
                        maximum_depth,
                        "entries",
                        invalid_type,
                    )
                maximum_depth = max(maximum_depth, child_depth)
                if maximum_depth > limits.max_depth:
                    return ScratchUsage(
                        byte_count,
                        entry_count,
                        limits.max_depth + 1,
                        "depth",
                        invalid_type,
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ScratchContainmentError("scratch entry identity changed during traversal") from exc
                if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
                    invalid_type = invalid_type or "link-or-reparse"
                elif stat.S_ISDIR(info.st_mode):
                    stack.append((Path(entry.path), child_depth))
                elif stat.S_ISREG(info.st_mode):
                    byte_count = min(limits.max_bytes + 1, byte_count + info.st_size)
                    if byte_count > limits.max_bytes:
                        return ScratchUsage(
                            limits.max_bytes + 1,
                            entry_count,
                            maximum_depth,
                            "bytes",
                            invalid_type,
                        )
                else:
                    invalid_type = invalid_type or "special-file"
    return ScratchUsage(byte_count, entry_count, maximum_depth, None, invalid_type)


def _scan_scratch_posix(
    root: Path,
    limits: ScratchLimits,
    *,
    root_identity: tuple[int, int],
) -> ScratchUsage:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise ScratchContainmentError("scratch root cannot be opened safely") from exc
    byte_count = entry_count = maximum_depth = 0
    invalid_type: str | None = None
    overage: str | None = None
    stack: list[tuple[int, Iterator[os.DirEntry[str]], int]] = []
    try:
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != root_identity:
            raise ScratchContainmentError("scratch root identity changed during traversal")
        stack.append((root_fd, os.scandir(root_fd), 0))
        while stack:
            directory_fd, entries, depth = stack[-1]
            entry = next(entries, None)
            if entry is None:
                entries.close()
                stack.pop()
                if directory_fd != root_fd:
                    os.close(directory_fd)
                continue
            child_depth = depth + 1
            entry_count += 1
            if entry_count > limits.max_entries:
                overage = "entries"
                break
            maximum_depth = max(maximum_depth, child_depth)
            if maximum_depth > limits.max_depth:
                overage = "depth"
                break
            try:
                information = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ScratchContainmentError(
                    "scratch entry identity changed during traversal"
                ) from exc
            if stat.S_ISLNK(information.st_mode):
                invalid_type = invalid_type or "link-or-reparse"
            elif stat.S_ISDIR(information.st_mode):
                try:
                    child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise ScratchContainmentError(
                        "scratch directory cannot be opened safely"
                    ) from exc
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        information.st_dev, information.st_ino
                    ):
                        raise ScratchContainmentError(
                            "scratch directory identity changed during traversal"
                        )
                    child_entries = os.scandir(child_fd)
                except BaseException:
                    os.close(child_fd)
                    raise
                stack.append((child_fd, child_entries, child_depth))
            elif stat.S_ISREG(information.st_mode):
                byte_count = min(limits.max_bytes + 1, byte_count + information.st_size)
                if byte_count > limits.max_bytes:
                    overage = "bytes"
                    break
            else:
                invalid_type = invalid_type or "special-file"
    except OSError as exc:
        raise ScratchContainmentError("scratch traversal could not verify an entry") from exc
    finally:
        for directory_fd, entries, _depth in reversed(stack):
            entries.close()
            if directory_fd != root_fd:
                os.close(directory_fd)
        os.close(root_fd)
    return ScratchUsage(byte_count, entry_count, maximum_depth, overage, invalid_type)


async def with_scratch_monitor(
    invocation: Awaitable[_T], root: Path, limits: ScratchLimits
) -> _T:
    """Stop and drain a driver on sampled overage, preserving cleanup failures."""
    process_done = asyncio.Event()

    async def invoke() -> _T:
        try:
            return await invocation
        finally:
            process_done.set()

    task = asyncio.create_task(invoke())

    async def terminate() -> None:
        await cancel_and_wait((task,))

    monitor = asyncio.create_task(
        monitor_scratch(root, limits, process_done=process_done, terminate=terminate)
    )
    try:
        await asyncio.wait({task, monitor}, return_when=asyncio.FIRST_COMPLETED)
        usage = await monitor
        if usage.overage is not None:
            raise ScratchLimitExceeded(f"scratch {usage.overage} limit exceeded")
        return await task
    finally:
        await cancel_and_wait((task, monitor))


async def monitor_scratch(
    root: Path | str,
    limits: ScratchLimits,
    *,
    process_done: asyncio.Event,
    terminate: Callable[[], Awaitable[None]],
) -> ScratchUsage:
    """Best-effort in-flight detector; the caller must still run a final scan."""

    latest = scan_scratch(root, limits)
    while not process_done.is_set():
        if latest.overage is not None:
            await terminate()
            return latest
        try:
            await asyncio.wait_for(process_done.wait(), timeout=0.25)
        except TimeoutError:
            latest = scan_scratch(root, limits)
    return latest


def require_final_scratch_within_limits(root: Path | str, limits: ScratchLimits) -> ScratchUsage:
    usage = scan_scratch(root, limits)
    if usage.overage is not None:
        raise ScratchLimitExceeded(f"scratch {usage.overage} limit exceeded")
    if usage.invalid_type is not None:
        raise ScratchContainmentError(f"scratch contains unsupported {usage.invalid_type}")
    return usage


def cleanup_reserved_tree(
    root: Path | str,
    *,
    timeout_seconds: float,
    expected_identity: tuple[int, int] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    root_path = Path(root)
    try:
        root_info = root_path.lstat()
    except FileNotFoundError:
        return
    identity = (root_info.st_dev, root_info.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise ScratchContainmentError("reserved root identity changed before cleanup")
    if not stat.S_ISDIR(root_info.st_mode) or _is_reparse(root_info):
        raise ScratchContainmentError("reserved root is not a non-reparse directory")
    deadline = clock() + timeout_seconds
    if os.name != "nt":
        _cleanup_reserved_tree_posix(
            root_path,
            identity=identity,
            deadline=deadline,
            clock=clock,
        )
        return
    stack: list[tuple[Path, os.ScandirIterator[str]]] = []
    try:
        stack.append((root_path, os.scandir(root_path)))
        while stack:
            if clock() > deadline:
                raise ScratchCleanupTimeout(
                    "reserved-tree cleanup exceeded its independent bound"
                )
            directory, entries = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                stack.pop()
                if directory == root_path:
                    current = directory.lstat()
                    if (current.st_dev, current.st_ino) != identity:
                        raise ScratchContainmentError(
                            "reserved root identity changed during cleanup"
                        )
                directory.rmdir()
                continue
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
                _unlink_reparse(path, info)
            elif stat.S_ISDIR(info.st_mode):
                try:
                    stack.append((path, os.scandir(path)))
                except OSError as exc:
                    raise ScratchContainmentError(
                        "reserved directory cannot be opened for cleanup"
                    ) from exc
            else:
                path.unlink()
    finally:
        for _, entries in stack:
            entries.close()
    if root_path.exists() or root_path.is_symlink():
        raise ScratchContainmentError("reserved root absence could not be proved")


def _cleanup_reserved_tree_posix(
    root: Path,
    *,
    identity: tuple[int, int],
    deadline: float,
    clock: Callable[[], float],
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise ScratchContainmentError("reserved root cannot be opened safely") from exc
    try:
        information = os.fstat(root_fd)
        if (information.st_dev, information.st_ino) != identity:
            raise ScratchContainmentError("reserved root identity changed before cleanup")
        _cleanup_directory_fd(root_fd, flags=flags, deadline=deadline, clock=clock)
        current = root.lstat()
        if (current.st_dev, current.st_ino) != identity:
            raise ScratchContainmentError("reserved root identity changed during cleanup")
    finally:
        os.close(root_fd)
    root.rmdir()
    if os.path.lexists(root):
        raise ScratchContainmentError("reserved root absence could not be proved")


def _cleanup_directory_fd(
    directory_fd: int,
    *,
    flags: int,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    root_entries = os.scandir(directory_fd)
    stack: list[tuple[int, int | None, str | None, Iterator[os.DirEntry[str]]]] = [
        (directory_fd, None, None, root_entries)
    ]
    try:
        while stack:
            current_fd, parent_fd, child_name, entries = stack[-1]
            if clock() > deadline:
                raise ScratchCleanupTimeout(
                    "reserved-tree cleanup exceeded its independent bound"
                )
            try:
                entry = next(entries)
            except StopIteration:
                entries.close()
                stack.pop()
                if parent_fd is not None and child_name is not None:
                    os.close(current_fd)
                    try:
                        os.rmdir(child_name, dir_fd=parent_fd)
                    except OSError as exc:
                        raise ScratchContainmentError(
                            "reserved child changed before directory removal"
                        ) from exc
                continue

            name = entry.name
            try:
                information = os.stat(
                    name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(information.st_mode) and not stat.S_ISLNK(
                information.st_mode
            ):
                try:
                    child_fd = os.open(name, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise ScratchContainmentError(
                        "reserved child directory cannot be opened safely"
                    ) from exc
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        information.st_dev,
                        information.st_ino,
                    ):
                        raise ScratchContainmentError(
                            "reserved child identity changed during cleanup"
                        )
                    child_entries = os.scandir(child_fd)
                except BaseException:
                    os.close(child_fd)
                    raise
                stack.append((child_fd, current_fd, name, child_entries))
            else:
                try:
                    os.unlink(name, dir_fd=current_fd)
                except OSError as exc:
                    raise ScratchContainmentError(
                        "reserved child changed before leaf removal"
                    ) from exc
    finally:
        for current_fd, parent_fd, _child_name, entries in reversed(stack):
            entries.close()
            if parent_fd is not None:
                os.close(current_fd)


def _unlink_reparse(path: Path, info: os.stat_result) -> None:
    try:
        path.unlink()
    except IsADirectoryError:
        path.rmdir()
    except PermissionError:
        if stat.S_ISDIR(info.st_mode):
            path.rmdir()
        else:
            raise


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
