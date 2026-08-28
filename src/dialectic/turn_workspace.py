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
    root_identity: tuple[int, int]
    control_identity: tuple[int, int]
    temporary_identity: tuple[int, int]

    @classmethod
    def create(cls, worktree: Path) -> "TurnWorkspace":
        root = worktree / ".dialectic-turn"
        if os.path.lexists(root):
            raise TurnWorkspaceError("reserved turn workspace already exists")
        root.mkdir(mode=0o700)
        control = root / "control"
        temporary = root / "tmp"
        control.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
        output = control / "output.json"
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        return cls(
            root=root,
            control=control,
            temporary=temporary,
            output=output,
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
            entries = list(os.scandir(self.control))
            if len(entries) != 1 or entries[0].name != self.output.name:
                raise TurnWorkspaceError("turn control directory contains unexpected entries")
            info = entries[0].stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or hard_link_count(self.output) != 1
                or info.st_size > limits.max_packet_bytes
            ):
                raise TurnWorkspaceError("turn control output identity or type is invalid")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise TurnWorkspaceError("turn control output owner changed")
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
