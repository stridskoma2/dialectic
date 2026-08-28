"""Pinned Git workspace creation and shared writable-turn validation."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from .contracts import FailureKind
from .filesystem import hard_link_count
from .locking import RepositoryIdentity, RepositoryIdentityError, resolve_repository_identity
from .schemas import LimitsSpec
from .store import RunHandle, RunStore

_GIT_CHUNK_BYTES = 65_536
_STRUCTURAL_OUTPUT_LIMIT = 16 * 1024 * 1024


class GitWorkflowError(RuntimeError):
    def __init__(self, kind: FailureKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


class GitCommandError(RuntimeError):
    def __init__(self, arguments: Sequence[str], returncode: int, stderr: bytes) -> None:
        self.arguments = tuple(arguments)
        self.returncode = returncode
        diagnostic = stderr[:4096].decode("utf-8", errors="replace").strip()
        super().__init__(diagnostic or f"git exited {returncode}")


class GitOutputLimitError(RuntimeError):
    pass


class GitObserverStopped(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitResult:
    stdout: bytes
    stderr: bytes
    returncode: int


class GitRunner:
    """Run controller-owned Git commands without hooks, helpers, or unbounded pipes."""

    def __init__(self, hooks_directory: Path, *, timeout_seconds: float = 30) -> None:
        self.hooks_directory = hooks_directory.resolve()
        self.timeout_seconds = timeout_seconds
        self.history: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        stdout_limit: int = _STRUCTURAL_OUTPUT_LIMIT,
        stderr_limit: int = _STRUCTURAL_OUTPUT_LIMIT,
        check: bool = True,
        pin_autocrlf: bool = True,
        stdout_observer: Callable[[bytes], bool] | None = None,
    ) -> GitResult:
        command = [
            "git",
            "-c",
            f"core.hooksPath={self.hooks_directory}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.pager=cat",
            "-c",
            "commit.gpgSign=false",
        ]
        if pin_autocrlf:
            command.extend(["-c", "core.autocrlf=false"])
        command.extend(arguments)
        self.history.append(tuple(command))
        environment = os.environ.copy()
        environment.update(
            {
                "LC_ALL": "C",
                "LANG": "C",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
            }
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        overflow = threading.Event()
        observer_stopped = threading.Event()
        stdout = bytearray()
        stderr = bytearray()

        def drain(
            stream,  # type: ignore[no-untyped-def]
            sink: bytearray,
            limit: int,
            observer: Callable[[bytes], bool] | None,
        ) -> None:
            while True:
                chunk = stream.read(_GIT_CHUNK_BYTES)
                if not chunk:
                    return
                if observer is not None and not observer(chunk):
                    observer_stopped.set()
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return
                remaining = limit + 1 - len(sink)
                if remaining > 0:
                    sink.extend(chunk[:remaining])
                if len(sink) > limit:
                    overflow.set()
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    return

        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, stdout_limit, stdout_observer),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, stderr_limit, None),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        if input_bytes is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(input_bytes)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        timed_out = False
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            returncode = -1
        finally:
            for reader in readers:
                reader.join(1)
        if any(reader.is_alive() for reader in readers):
            raise GitCommandError(arguments, -1, b"git stream reader cleanup failed")
        if timed_out:
            raise GitCommandError(arguments, -1, b"git command timed out")
        if observer_stopped.is_set():
            raise GitObserverStopped("git output observer stopped the subprocess")
        if overflow.is_set():
            raise GitOutputLimitError("git output exceeded its configured bound")
        result = GitResult(bytes(stdout), bytes(stderr), returncode)
        if check and returncode != 0:
            raise GitCommandError(arguments, returncode, result.stderr)
        return result


@dataclass(frozen=True, slots=True)
class RepositoryBaseline:
    original_worktree: Path
    common_directory: Path
    identity: RepositoryIdentity
    original_branch: str | None
    base_sha: str
    main_sha: str | None
    status_bytes: bytes


@dataclass(frozen=True, slots=True)
class LinkedWorkspace:
    baseline: RepositoryBaseline
    branch: str
    path: Path


class GitWorkspace:
    def __init__(self, runner: GitRunner, state_root: Path) -> None:
        self.runner = runner
        self.state_root = state_root

    def preflight(self, repository: Path) -> RepositoryBaseline:
        try:
            original = repository.resolve(strict=True)
        except OSError as exc:
            raise GitWorkflowError("UNSUPPORTED_REPOSITORY", "repository path does not exist") from exc
        inside = self.runner.run(
            ["rev-parse", "--is-inside-work-tree"], cwd=original, check=False
        )
        if inside.returncode != 0 or inside.stdout.strip() != b"true":
            raise GitWorkflowError(
                "UNSUPPORTED_REPOSITORY", "path is not a non-bare Git working tree"
            )
        bare = self.runner.run(["rev-parse", "--is-bare-repository"], cwd=original)
        if bare.stdout.strip() != b"false":
            raise GitWorkflowError("UNSUPPORTED_REPOSITORY", "bare repositories are unsupported")
        common_raw = self.runner.run(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=original
        ).stdout
        try:
            common = Path(common_raw.decode("utf-8", errors="strict").strip()).resolve(strict=True)
            identity = resolve_repository_identity(common)
        except (UnicodeError, OSError, RepositoryIdentityError) as exc:
            raise GitWorkflowError(
                "PREFLIGHT_FAILED", "stable repository identity cannot be established"
            ) from exc
        status_bytes = self.runner.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=original,
            pin_autocrlf=False,
        ).stdout
        if status_bytes:
            diagnostic = status_bytes[:512].decode("utf-8", errors="replace")
            raise GitWorkflowError(
                "UNSUPPORTED_REPOSITORY",
                f"original repository working tree is dirty: {diagnostic}",
            )
        sparse = self.runner.run(
            ["config", "--bool", "--get", "core.sparseCheckout"],
            cwd=original,
            check=False,
        )
        if sparse.returncode == 0 and sparse.stdout.strip() == b"true":
            raise GitWorkflowError("UNSUPPORTED_REPOSITORY", "sparse checkout is unsupported")
        stage = self.runner.run(["ls-files", "--stage", "-z"], cwd=original).stdout
        if _contains_gitlink(stage):
            raise GitWorkflowError("UNSUPPORTED_REPOSITORY", "tracked gitlinks are unsupported")
        tracked = _parse_nul_paths(self.runner.run(["ls-files", "-z"], cwd=original).stdout)
        if any(_is_reserved_path(path) for path in tracked):
            raise GitWorkflowError(
                "UNSUPPORTED_REPOSITORY", "tracked .dialectic-turn content is unsupported"
            )
        reserved = original / ".dialectic-turn"
        if os.path.lexists(reserved):
            raise GitWorkflowError(
                "UNSUPPORTED_REPOSITORY", "on-disk .dialectic-turn content is unsupported"
            )
        self._reject_filtered_paths(original, tracked)
        branch_result = self.runner.run(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=original, check=False
        )
        original_branch = (
            branch_result.stdout.decode("utf-8", errors="strict").strip()
            if branch_result.returncode == 0
            else None
        )
        base_sha = _decode_sha(self.runner.run(["rev-parse", "HEAD"], cwd=original).stdout)
        main_result = self.runner.run(
            ["rev-parse", "--verify", "refs/heads/main"], cwd=original, check=False
        )
        main_sha = _decode_sha(main_result.stdout) if main_result.returncode == 0 else None
        return RepositoryBaseline(
            original_worktree=original,
            common_directory=common,
            identity=identity,
            original_branch=original_branch,
            base_sha=base_sha,
            main_sha=main_sha,
            status_bytes=status_bytes,
        )

    def create_linked_worktree(
        self, baseline: RepositoryBaseline, run_id: str
    ) -> LinkedWorkspace:
        branch = f"dialectic/{run_id}"
        worktrees_root = self.state_root / "worktrees"
        worktrees_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = worktrees_root / run_id
        if os.path.lexists(path):
            raise GitWorkflowError("PREFLIGHT_FAILED", "isolated worktree path already exists")
        try:
            self.runner.run(
                ["worktree", "add", "-b", branch, str(path), baseline.base_sha],
                cwd=baseline.original_worktree,
            )
            resolved = path.resolve(strict=True)
            top = self.runner.run(
                ["rev-parse", "--show-toplevel"], cwd=resolved
            ).stdout.decode("utf-8", errors="strict").strip()
            if Path(top).resolve(strict=True) != resolved:
                raise GitWorkflowError(
                    "PREFLIGHT_FAILED", "isolated worktree identity verification failed"
                )
        except GitWorkflowError:
            raise
        except (GitCommandError, OSError, UnicodeError) as exc:
            raise GitWorkflowError("PREFLIGHT_FAILED", "isolated worktree creation failed") from exc
        return LinkedWorkspace(baseline=baseline, branch=branch, path=resolved)

    def _reject_filtered_paths(self, repository: Path, paths: Sequence[bytes]) -> None:
        if not paths:
            return
        raw = b"\0".join(paths) + b"\0"
        output = self.runner.run(
            ["check-attr", "-z", "--stdin", "filter"],
            cwd=repository,
            input_bytes=raw,
        ).stdout
        for _, attribute, value in _parse_attribute_records(output):
            if attribute != b"filter" or value != b"unspecified":
                raise GitWorkflowError(
                    "UNSUPPORTED_REPOSITORY", "tracked clean/process filters are unsupported"
                )


@dataclass(frozen=True, slots=True)
class ValidatedChange:
    head_sha: str
    complete_diff: bytes
    complete_diff_sha256: str
    repair_delta: bytes | None
    repair_delta_sha256: str | None
    commit_created: bool
    changed_paths: tuple[str, ...]


class ChangeValidator:
    def __init__(
        self,
        *,
        runner: GitRunner,
        store: RunStore,
        handle: RunHandle,
        workspace: LinkedWorkspace,
        limits: LimitsSpec,
        after_commit: Callable[[Path], None] | None = None,
    ) -> None:
        self.runner = runner
        self.store = store
        self.handle = handle
        self.workspace = workspace
        self.limits = limits
        self.after_commit = after_commit

    def validate_initial(self) -> ValidatedChange:
        return self._validate(turn="initial", review_sha=None)

    def validate_repair(self, review_sha: str) -> ValidatedChange:
        return self._validate(turn="repair", review_sha=review_sha)

    def _validate(
        self,
        *,
        turn: Literal["initial", "repair"],
        review_sha: str | None,
    ) -> ValidatedChange:
        worktree = self.workspace.path
        if os.path.lexists(worktree / ".dialectic-turn"):
            raise GitWorkflowError(
                "INTERNAL_ERROR", "reserved turn workspace remains before Git validation"
            )
        raw_paths = self._enumerate_changed_paths()
        decoded_paths, aggregate_size = self._inspect_candidates(raw_paths)
        if aggregate_size > self.limits.max_candidate_change_bytes:
            raise GitWorkflowError(
                "UNSUPPORTED_CHANGE", "candidate aggregate logical size exceeds configured bound"
            )
        self._reject_candidate_filters(raw_paths)
        self.runner.run(["add", "-A", "--", "."], cwd=worktree)
        staged_entries = self.runner.run(["ls-files", "--stage", "-z"], cwd=worktree).stdout
        if _contains_gitlink(staged_entries):
            raise GitWorkflowError("UNSUPPORTED_CHANGE", "index contains a gitlink")
        numstat = self.runner.run(
            [
                "diff",
                "--cached",
                "--numstat",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                self.workspace.baseline.base_sha,
                "--",
            ],
            cwd=worktree,
        ).stdout
        if _numstat_has_binary(numstat):
            raise GitWorkflowError("UNSUPPORTED_CHANGE", "binary changes are unsupported")
        complete = self._staged_diff(self.workspace.baseline.base_sha)
        if turn == "initial" and not complete:
            raise GitWorkflowError("NO_CHANGES", "initial driver turn produced no changes")
        repair_delta = self._staged_diff(review_sha) if review_sha is not None else None
        complete_hash = hashlib.sha256(complete).hexdigest()
        repair_hash = hashlib.sha256(repair_delta).hexdigest() if repair_delta is not None else None
        if turn == "initial":
            self.store.write_artifact(self.handle, "git/initial.diff", complete)
            self.store.write_artifact(
                self.handle, "git/initial.diff.sha256", (complete_hash + "\n").encode("ascii")
            )
            delta_nonempty = bool(complete)
        else:
            assert repair_delta is not None and repair_hash is not None
            self.store.write_artifact(self.handle, "git/repair.delta.diff", repair_delta)
            self.store.write_artifact(
                self.handle,
                "git/repair.delta.diff.sha256",
                (repair_hash + "\n").encode("ascii"),
            )
            self.store.write_artifact(self.handle, "git/final.diff", complete)
            self.store.write_artifact(
                self.handle, "git/final.diff.sha256", (complete_hash + "\n").encode("ascii")
            )
            delta_nonempty = bool(repair_delta)
        commit_created = False
        if delta_nonempty:
            self.runner.run(
                [
                    "-c",
                    "user.name=Dialectic",
                    "-c",
                    "user.email=dialectic@localhost",
                    "commit",
                    "--no-verify",
                    "-m",
                    f"dialectic: {turn} {self.handle.run_id}",
                ],
                cwd=worktree,
            )
            commit_created = True
            if self.after_commit is not None:
                self.after_commit(worktree)
        head_sha = _decode_sha(self.runner.run(["rev-parse", "HEAD"], cwd=worktree).stdout)
        committed = self._committed_diff(self.workspace.baseline.base_sha)
        if committed != complete or hashlib.sha256(committed).hexdigest() != complete_hash:
            raise GitWorkflowError(
                "INTERNAL_ERROR", "committed diff differs from validated staged bytes"
            )
        status = self.runner.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=worktree
        ).stdout
        if status:
            raise GitWorkflowError(
                "INTERNAL_ERROR", "post-validation worktree or index is not clean"
            )
        return ValidatedChange(
            head_sha=head_sha,
            complete_diff=complete,
            complete_diff_sha256=complete_hash,
            repair_delta=repair_delta,
            repair_delta_sha256=repair_hash,
            commit_created=commit_created,
            changed_paths=tuple(decoded_paths),
        )

    def _enumerate_changed_paths(self) -> tuple[bytes, ...]:
        commands = (
            ["diff", "--cached", "--name-only", "-z", "--no-renames", "HEAD", "--"],
            ["diff", "--name-only", "-z", "--no-renames", "--"],
            ["ls-files", "--others", "--exclude-standard", "-z", "--"],
        )
        unique: set[bytes] = set()
        path_output_limit = min(
            _STRUCTURAL_OUTPUT_LIMIT,
            self.limits.max_candidate_change_bytes + 1,
        )
        for command in commands:
            partial = bytearray()
            observer_error: list[GitWorkflowError] = []

            def observe(chunk: bytes) -> bool:
                partial.extend(chunk)
                while True:
                    delimiter = partial.find(0)
                    if delimiter < 0:
                        return True
                    record = bytes(partial[:delimiter])
                    del partial[: delimiter + 1]
                    if not record:
                        observer_error.append(
                            GitWorkflowError("UNSUPPORTED_CHANGE", "Git emitted an empty path")
                        )
                        return False
                    unique.add(record)
                    if len(unique) > self.limits.max_changed_paths:
                        observer_error.append(
                            GitWorkflowError(
                                "UNSUPPORTED_CHANGE",
                                "changed path count exceeds configured bound",
                            )
                        )
                        return False

            try:
                self.runner.run(
                    command,
                    cwd=self.workspace.path,
                    stdout_limit=path_output_limit,
                    stdout_observer=observe,
                )
            except GitObserverStopped as exc:
                if observer_error:
                    raise observer_error[0] from exc
                raise GitWorkflowError(
                    "INTERNAL_ERROR", "changed-path observer stopped unexpectedly"
                ) from exc
            except GitOutputLimitError as exc:
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", "changed-path enumeration exceeded its byte bound"
                ) from exc
            if partial:
                raise GitWorkflowError(
                    "INTERNAL_ERROR", "Git path output is not NUL terminated"
                )
        return tuple(sorted(unique))

    def _inspect_candidates(self, raw_paths: Iterable[bytes]) -> tuple[list[str], int]:
        decoded: list[str] = []
        aggregate = 0
        for raw in raw_paths:
            try:
                relative = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", "changed path is not strict UTF-8"
                ) from exc
            path = self._candidate_path(relative)
            try:
                info = path.lstat()
            except FileNotFoundError:
                decoded.append(relative)
                continue
            attributes = getattr(info, "st_file_attributes", 0)
            is_reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if is_reparse or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", f"changed path has unsupported type: {relative}"
                )
            if hard_link_count(path) != 1:
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", f"changed path is multiply linked: {relative}"
                )
            if info.st_size > self.limits.max_changed_regular_file_bytes:
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", f"changed file exceeds individual bound: {relative}"
                )
            aggregate = min(
                self.limits.max_candidate_change_bytes + 1,
                aggregate + info.st_size,
            )
            decoded.append(relative)
        return decoded, aggregate

    def _candidate_path(self, relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise GitWorkflowError("UNSUPPORTED_CHANGE", "changed path escapes the worktree")
        parent = self.workspace.path
        for component in relative_path.parts[:-1]:
            parent = parent / component
            try:
                information = parent.lstat()
            except FileNotFoundError:
                break
            attributes = getattr(information, "st_file_attributes", 0)
            reparse = bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if reparse or stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(
                information.st_mode
            ):
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", "changed path has an unsafe parent component"
                )
        return self.workspace.path / relative_path

    def _reject_candidate_filters(self, raw_paths: Sequence[bytes]) -> None:
        if not raw_paths:
            return
        output = self.runner.run(
            ["check-attr", "-z", "--stdin", "filter"],
            cwd=self.workspace.path,
            input_bytes=b"\0".join(raw_paths) + b"\0",
        ).stdout
        for path, attribute, value in _parse_attribute_records(output):
            if attribute != b"filter" or value != b"unspecified":
                display = path.decode("utf-8", errors="replace")
                raise GitWorkflowError(
                    "UNSUPPORTED_CHANGE", f"changed path has a clean/process filter: {display}"
                )

    def _staged_diff(self, reference: str | None) -> bytes:
        assert reference is not None
        try:
            output = self.runner.run(
                _diff_arguments(reference, cached=True),
                cwd=self.workspace.path,
                stdout_limit=self.limits.max_diff_bytes,
                stderr_limit=self.limits.max_agent_stderr_bytes,
            ).stdout
        except GitOutputLimitError as exc:
            raise GitWorkflowError("DIFF_TOO_LARGE", "staged diff exceeds max_diff_bytes") from exc
        return _strict_diff(output)

    def _committed_diff(self, reference: str) -> bytes:
        try:
            output = self.runner.run(
                _diff_arguments(reference, cached=False),
                cwd=self.workspace.path,
                stdout_limit=self.limits.max_diff_bytes,
                stderr_limit=self.limits.max_agent_stderr_bytes,
            ).stdout
        except GitOutputLimitError as exc:
            raise GitWorkflowError("DIFF_TOO_LARGE", "committed diff exceeds max_diff_bytes") from exc
        return _strict_diff(output)


def _diff_arguments(reference: str, *, cached: bool) -> list[str]:
    arguments = [
        "-c",
        "core.quotePath=false",
        "-c",
        "diff.external=",
        "-c",
        "diff.algorithm=histogram",
        "-c",
        "diff.indentHeuristic=false",
        "-c",
        "color.ui=false",
        "--no-pager",
        "diff",
    ]
    if cached:
        arguments.append("--cached")
    arguments.extend(
        [
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            reference,
            "--",
        ]
    )
    return arguments


def _strict_diff(raw: bytes) -> bytes:
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitWorkflowError("UNSUPPORTED_CHANGE", "diff is not strict UTF-8") from exc
    return raw


def _parse_nul_paths(raw: bytes) -> tuple[bytes, ...]:
    if not raw:
        return ()
    if not raw.endswith(b"\0"):
        raise GitWorkflowError("INTERNAL_ERROR", "Git path output is not NUL terminated")
    records = raw[:-1].split(b"\0")
    if any(not record for record in records):
        raise GitWorkflowError("UNSUPPORTED_CHANGE", "Git emitted an empty path")
    return tuple(records)


def _parse_attribute_records(raw: bytes) -> tuple[tuple[bytes, bytes, bytes], ...]:
    if not raw:
        return ()
    if not raw.endswith(b"\0"):
        raise GitWorkflowError("INTERNAL_ERROR", "Git attribute output is not NUL terminated")
    parts = raw[:-1].split(b"\0")
    if len(parts) % 3:
        raise GitWorkflowError("INTERNAL_ERROR", "Git attribute output has invalid arity")
    return tuple(zip(parts[0::3], parts[1::3], parts[2::3], strict=True))


def _contains_gitlink(raw: bytes) -> bool:
    if not raw:
        return False
    for record in _parse_nul_paths(raw):
        mode = record.split(b" ", 1)[0]
        if mode == b"160000":
            return True
    return False


def _numstat_has_binary(raw: bytes) -> bool:
    if not raw:
        return False
    for record in _parse_nul_paths(raw):
        parts = record.split(b"\t", 2)
        if len(parts) != 3:
            raise GitWorkflowError("INTERNAL_ERROR", "Git numstat output is malformed")
        if parts[0] == b"-" or parts[1] == b"-":
            return True
    return False


def _is_reserved_path(raw: bytes) -> bool:
    return raw == b".dialectic-turn" or raw.startswith(b".dialectic-turn/")


def _decode_sha(raw: bytes) -> str:
    value = raw.decode("ascii", errors="strict").strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise GitWorkflowError("INTERNAL_ERROR", "Git returned an invalid object id")
    return value
