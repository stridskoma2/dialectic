"""Bounded projections of retained runs, with no bootstrap or execution authority."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .audit import OfflineRunAuditor
from .schemas import RunAuditReport, RunRecord
from .store import default_state_root, validate_run_id

MAX_HISTORY_RUNS = 10_000
MAX_HISTORY_RESULTS = 200
MAX_HISTORY_ARTIFACTS = 2_000
MAX_HISTORY_FILE_BYTES = 1_048_576
MAX_HISTORY_TEXT_BYTES = 33_554_432


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    run_id: str
    record: RunRecord | None
    prompt: str
    warnings: tuple[str, ...]

    @property
    def title(self) -> str:
        return " ".join(self.prompt.split())[:160] or "No retained prompt"


@dataclass(frozen=True, slots=True)
class HistoryListing:
    entries: tuple[HistoryEntry, ...]
    limited: bool


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    entry: HistoryEntry
    artifact_dir: Path
    artifacts: tuple[tuple[str, int], ...]
    contents: dict[str, str]
    warnings: tuple[str, ...]


class RunHistory:
    """Read existing state without constructing the writable RunStore."""

    def __init__(self, state_root: Path | None = None) -> None:
        self.state_root = (state_root or default_state_root()).absolute()
        self.runs_root = self.state_root / "runs"
        self.capability_attestations_root = self.state_root / "capability-attestations"

    def _directory(self, path: Path) -> None:
        _require_directory(self.state_root)
        current = self.state_root
        for component in path.relative_to(self.state_root).parts:
            current /= component
            _require_directory(current)

    def read_artifact(self, run_id: str, relative: str) -> str:
        """Read one non-linked regular UTF-8 artifact under a stable size ceiling."""
        validate_run_id(run_id)
        name = PurePosixPath(relative)
        if (
            not relative or name.is_absolute() or ".." in name.parts
            or "\\" in relative or ":" in relative or not name.parts
        ):
            raise ValueError("invalid artifact path")
        path = self.runs_root / run_id / Path(*name.parts)
        self._directory(path.parent)
        expected = path.lstat()
        _require_file(expected)
        if expected.st_size > MAX_HISTORY_FILE_BYTES:
            raise ValueError("artifact exceeds the history preview byte limit")
        flags = (
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            _require_file(opened)
            if _identity(expected) != _identity(opened):
                raise ValueError("artifact changed before reading")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_HISTORY_FILE_BYTES + 1)
            self._directory(path.parent)
            if (
                _identity(opened) != _identity(os.fstat(descriptor))
                or _identity(opened) != _identity(path.lstat())
                or len(data) != opened.st_size
            ):
                raise ValueError("artifact changed while reading")
            return data.decode("utf-8")
        finally:
            os.close(descriptor)

    def _entry(self, run_id: str) -> HistoryEntry:
        warnings: list[str] = []
        record = None
        try:
            record = RunRecord.model_validate_json(self.read_artifact(run_id, "run.json"))
            if record.run_id != run_id:
                raise ValueError("run ID mismatch")
        except (OSError, ValueError) as exc:
            record = None
            warnings.append(f"run.json unavailable ({type(exc).__name__}); audit for details.")
        prompt = ""
        for relative in ("input/prompt.md", "input/task.md"):
            try:
                prompt = self.read_artifact(run_id, relative)
                break
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                warnings.append(f"{relative} unavailable ({type(exc).__name__}).")
        return HistoryEntry(run_id, record, prompt, tuple(warnings))

    def list_runs(self, query: str = "") -> HistoryListing:
        if not self.runs_root.exists():
            return HistoryListing((), False)
        self._directory(self.runs_root)
        names: list[str] = []
        limited = False
        with os.scandir(self.runs_root) as directories:
            for index, entry in enumerate(directories):
                if index >= MAX_HISTORY_RUNS:
                    limited = True
                    break
                try:
                    validate_run_id(entry.name)
                except ValueError:
                    continue
                names.append(entry.name)
        matches: list[HistoryEntry] = []
        needle = query.strip().casefold()
        for run_id in sorted(names, reverse=True):
            item = self._entry(run_id)
            record = item.record
            searchable = " ".join((
                run_id, item.prompt, record.mode if record else "",
                record.status if record else "unavailable",
                (record.code_outcome or record.consensus_outcome or "") if record else "",
            ))
            if needle and needle not in searchable.casefold():
                continue
            if len(matches) == MAX_HISTORY_RESULTS:
                limited = True
                break
            matches.append(item)
        return HistoryListing(tuple(matches), limited)

    def load_run(self, run_id: str) -> HistorySnapshot:
        validate_run_id(run_id)
        root = self.runs_root / run_id
        self._directory(root)
        entry = self._entry(run_id)
        warnings = list(entry.warnings)
        artifacts: list[tuple[str, int]] = []
        pending = [root]
        visited = 0
        while pending and visited < MAX_HISTORY_ARTIFACTS:
            directory = pending.pop()
            try:
                self._directory(directory)
                with os.scandir(directory) as children:
                    for child in children:
                        visited += 1
                        if visited > MAX_HISTORY_ARTIFACTS:
                            break
                        path = Path(child.path)
                        relative = path.relative_to(root).as_posix()
                        # Windows DirEntry metadata can omit the hard-link count.
                        info = path.lstat()
                        if _linked(info):
                            warnings.append(f"Skipped linked/reparse artifact: {relative}")
                        elif stat.S_ISDIR(info.st_mode):
                            if len(path.relative_to(root).parts) < 16:
                                pending.append(path)
                            else:
                                warnings.append(f"Skipped deeply nested directory: {relative}")
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            artifacts.append((relative, info.st_size))
                        else:
                            warnings.append(f"Skipped unsafe artifact: {relative}")
            except OSError as exc:
                warnings.append(f"Directory unavailable ({type(exc).__name__}).")
        if pending or visited >= MAX_HISTORY_ARTIFACTS:
            warnings.append("Artifact listing reached its entry limit.")
        contents: dict[str, str] = {}
        total = 0
        for relative, size in sorted(artifacts):
            if relative not in {
                "input/prompt.md", "input/task.md", "input/config.redacted.json", "summary.md",
            } and not (
                relative.startswith("turns/") and relative.endswith(".attempt.json")
                or relative.startswith("research/sources/") and relative.endswith(".json")
            ):
                continue
            if total + size > MAX_HISTORY_TEXT_BYTES:
                warnings.append("Session previews reached their total byte limit.")
                break
            try:
                contents[relative] = self.read_artifact(run_id, relative)
                total += size
            except (OSError, ValueError) as exc:
                warnings.append(f"{relative} unavailable ({type(exc).__name__}).")
        return HistorySnapshot(entry, root, tuple(sorted(artifacts)), contents, tuple(warnings))

    def audit_run(self, run_id: str) -> RunAuditReport:
        self._directory(self.runs_root)
        return OfflineRunAuditor(self).audit(run_id)


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _require_directory(path: Path) -> None:
    info = path.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("history path is not a non-reparse directory")


def _require_file(info: os.stat_result) -> None:
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("history artifact is not a non-linked regular file")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink
