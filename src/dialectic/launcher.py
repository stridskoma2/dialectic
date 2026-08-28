"""Bounded executable-plus-argument launch-plan construction."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .contracts import MAX_ARG_BYTES

_UNSAFE_BATCH = re.compile(r"[%!^&|<>\x00-\x1f\x7f]")
_SAFE_BATCH_ARGUMENT = re.compile(r"^[A-Za-z0-9._:/@+\[\]=,-]+$")


class LaunchPlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DirectLaunchSpec:
    executable: Path
    arguments: tuple[str, ...]
    launch_kind: str = "direct"


@dataclass(frozen=True, slots=True)
class WindowsBatchLaunchSpec:
    shim: Path
    arguments: tuple[str, ...]
    spawned_root_executable: Path
    launch_kind: str = "windows-batch-shim"

    @property
    def root_arguments(self) -> tuple[str, ...]:
        inner = " ".join((_quote_batch_path(self.shim), *self.arguments))
        return ("/d", "/q", "/v:off", "/s", "/c", f'"{inner}"')


LaunchSpec = DirectLaunchSpec | WindowsBatchLaunchSpec


def resolve_executable(
    executable_name: str,
    arguments: list[str] | tuple[str, ...],
    *,
    which: Callable[[str], str | None] = shutil.which,
    windows: bool | None = None,
    system_directory: Path | str | None = None,
) -> LaunchSpec:
    resolved = which(executable_name)
    if resolved is None:
        raise LaunchPlanError(f"executable is unavailable: {executable_name}")
    return build_launch_spec(
        Path(resolved).absolute(),
        arguments,
        windows=windows,
        system_directory=system_directory,
    )


def build_launch_spec(
    resolved_executable: Path | str,
    arguments: list[str] | tuple[str, ...],
    *,
    windows: bool | None = None,
    system_directory: Path | str | None = None,
) -> LaunchSpec:
    executable = Path(resolved_executable).absolute()
    args = tuple(arguments)
    validate_argv(args)
    is_windows = os.name == "nt" if windows is None else windows
    if not is_windows:
        return DirectLaunchSpec(executable, args)
    extension = executable.suffix.casefold()
    if extension in {".exe", ".com"}:
        return DirectLaunchSpec(executable, args)
    if extension not in {".cmd", ".bat"}:
        raise LaunchPlanError("Windows launcher must resolve to .exe, .com, .cmd, or .bat")
    if _UNSAFE_BATCH.search(str(executable)):
        raise LaunchPlanError("Windows batch shim path contains unsafe cmd.exe characters")
    for argument in args:
        if not _SAFE_BATCH_ARGUMENT.fullmatch(argument):
            raise LaunchPlanError("Windows batch argument is outside the constrained grammar")
    system_root = Path(system_directory) if system_directory is not None else _system_directory()
    cmd = (system_root / "cmd.exe").absolute()
    spec = WindowsBatchLaunchSpec(executable, args, cmd)
    validate_argv(spec.root_arguments)
    return spec


def validate_argv(arguments: tuple[str, ...] | list[str]) -> None:
    for index, argument in enumerate(arguments):
        if "\x00" in argument:
            raise LaunchPlanError(f"argv[{index}] contains NUL")
        if len(argument.encode("utf-8")) > MAX_ARG_BYTES:
            raise LaunchPlanError(f"argv[{index}] exceeds 4096 UTF-8 bytes")


def _quote_batch_path(path: Path) -> str:
    value = str(path)
    if '"' in value or _UNSAFE_BATCH.search(value):
        raise LaunchPlanError("batch shim path cannot be encoded safely")
    return f'"{value}"'


def _system_directory() -> Path:
    if os.name != "nt":
        raise LaunchPlanError("GetSystemDirectoryW is available only on Windows")
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError()
    return Path(buffer.value)
