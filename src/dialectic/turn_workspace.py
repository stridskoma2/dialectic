"""Per-turn reserved driver workspace with identity-bound safe cleanup."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .filesystem import hard_link_count
from .schemas import LimitsSpec
from .scratch import (
    ScratchCleanupTimeout,
    ScratchContainmentError,
    ScratchLimitExceeded,
    ScratchLimits,
    cleanup_reserved_tree,
    require_final_scratch_within_limits,
)


class TurnWorkspaceError(RuntimeError):
    pass


class TurnWorkspaceCleanupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnWorkspace:
    root: Path
    control: Path
    temporary: Path
    output: Path
    schema: Path | None
    root_identity: tuple[int, int]
    control_identity: tuple[int, int]
    temporary_identity: tuple[int, int]

    @classmethod
    def create(
        cls, worktree: Path, *, output_schema_bytes: bytes | None = None
    ) -> "TurnWorkspace":
        root = worktree / ".dialectic-turn"
        if os.path.lexists(root):
            raise TurnWorkspaceError("reserved turn workspace already exists")
        root.mkdir(mode=0o700)
        control = root / "control"
        temporary = root / "tmp"
        control.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
        output = control / "output.json"
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(output, create_flags, 0o600)
        os.close(descriptor)
        schema: Path | None = None
        if output_schema_bytes is not None:
            schema = control / "output-schema.json"
            descriptor = os.open(schema, create_flags, 0o600)
            try:
                remaining = memoryview(output_schema_bytes)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("failed to write turn output schema")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return cls(
            root=root,
            control=control,
            temporary=temporary,
            output=output,
            schema=schema,
            root_identity=_identity(root),
            control_identity=_identity(control),
            temporary_identity=_identity(temporary),
        )

    def verify_and_cleanup(self, limits: LimitsSpec) -> None:
        validation_error: Exception | None = None
        try:
            _require_same_directory(self.root, self.root_identity, "turn scratch root")
            _require_same_directory(self.control, self.control_identity, "turn control directory")
            _require_same_directory(self.temporary, self.temporary_identity, "turn tmp directory")
            entries = {entry.name: entry for entry in os.scandir(self.control)}
            expected = {self.output.name}
            if self.schema is not None:
                expected.add(self.schema.name)
            if set(entries) != expected:
                raise TurnWorkspaceError("turn control directory contains unexpected entries")
            for path in (self.output, self.schema):
                if path is not None:
                    _require_control_file(entries[path.name], path, limits)
            require_final_scratch_within_limits(
                self.temporary,
                ScratchLimits(
                    limits.max_turn_scratch_bytes,
                    limits.max_turn_scratch_entries,
                    limits.max_turn_scratch_depth,
                ),
            )
        except (OSError, ScratchContainmentError, ScratchLimitExceeded, TurnWorkspaceError) as exc:
            validation_error = exc
        try:
            cleanup_reserved_tree(
                self.root,
                timeout_seconds=limits.turn_cleanup_seconds,
                expected_identity=self.root_identity,
            )
        except (OSError, ScratchContainmentError, ScratchCleanupTimeout) as exc:
            raise TurnWorkspaceCleanupError("reserved turn workspace cleanup failed") from exc
        if validation_error is not None:
            if isinstance(validation_error, ScratchLimitExceeded):
                raise validation_error
            raise TurnWorkspaceError("reserved turn workspace validation failed") from validation_error


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise TurnWorkspaceError("dynamic turn object is not a directory")
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise TurnWorkspaceError("dynamic turn object is a reparse point")
    return info.st_dev, info.st_ino


def _require_same_directory(
    path: Path, expected: tuple[int, int], description: str
) -> None:
    if _identity(path) != expected:
        raise TurnWorkspaceError(f"{description} identity changed")


def _require_control_file(entry: os.DirEntry[str], path: Path, limits: LimitsSpec) -> None:
    info = entry.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or hard_link_count(path) != 1
        or info.st_size > limits.max_packet_bytes
    ):
        raise TurnWorkspaceError("turn control file identity or type is invalid")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise TurnWorkspaceError("turn control file owner changed")
