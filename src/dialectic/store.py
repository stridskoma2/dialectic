"""Private run bootstrap and canonical atomic artifact persistence."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from platformdirs import user_state_path
from pydantic import BaseModel, ValidationError

from .contracts import RUN_ID_PATTERN, RunMode
from .contracts import ARTIFACT_SCHEMA_VERSION, TOOL_VERSION
from .schemas import (
    CapabilityAttestationArtifact,
    EventRecord,
    RunRecord,
    SummaryRecord,
    WorkspaceRecord,
)

import re

_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


class BootstrapError(RuntimeError):
    """Secure bootstrap failed before a durable run could be published."""


class RunNotFoundError(LookupError):
    pass


class StateCorruptError(RuntimeError):
    pass


class InvalidRunIdError(ValueError):
    pass


class ArtifactExistsError(FileExistsError):
    pass


class _PublishCollision(FileExistsError):
    pass


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Opaque authority for mutating exactly one securely published run."""

    run_id: str
    path: Path
    _store_nonce: str


def default_state_root() -> Path:
    return Path(user_state_path("dialectic", appauthor=False))


def default_role_directories_root() -> Path:
    if os.name != "nt":
        return default_state_root() / "role-directories"
    from win32com.shell import shell, shellcon

    public_documents = Path(
        shell.SHGetFolderPath(0, shellcon.CSIDL_COMMON_DOCUMENTS, None, 0)
    ).resolve(strict=True)
    return public_documents.parent.resolve(strict=True)


def generate_run_id(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = base64.b32encode(secrets.token_bytes(7)).decode("ascii").lower()[:10]
    return f"{timestamp}-{suffix}"


def validate_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise InvalidRunIdError("run id does not match the canonical grammar")
    return run_id


def canonical_json_bytes(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude_none=False, exclude_unset=False)
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def artifact_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RunStore:
    def __init__(
        self,
        state_root: Path | str | None = None,
        *,
        run_id_factory: Callable[[], str] = generate_run_id,
        bootstrap_suffix_factory: Callable[[], str] | None = None,
        publisher: Callable[[Path, Path], None] | None = None,
        privacy_verifier: Callable[[Path, Path], None] | None = None,
        role_directories_root: Path | str | None = None,
    ) -> None:
        using_default_state_root = state_root is None
        self.state_root = Path(state_root) if state_root is not None else default_state_root()
        self.runs_root = self.state_root / "runs"
        self.capability_attestations_root = self.state_root / "capability-attestations"
        self._flat_role_directories = (
            role_directories_root is None and os.name == "nt" and using_default_state_root
        )
        configured_role_root = role_directories_root
        if self._flat_role_directories:
            configured_role_root = default_role_directories_root()
        self.role_directories_root = (
            Path(configured_role_root) if configured_role_root is not None else None
        )
        self._run_id_factory = run_id_factory
        self._suffix_factory = bootstrap_suffix_factory or (lambda: secrets.token_hex(6))
        self._publisher = publisher or _publish_directory_no_replace
        self._privacy_verifier = privacy_verifier or _verify_private_directory
        self._nonce = secrets.token_hex(16)
        _ensure_private_directory(self.state_root)
        _ensure_private_directory(self.runs_root)
        _ensure_private_directory(self.capability_attestations_root)
        if self.role_directories_root is not None and not self._flat_role_directories:
            _ensure_private_directory(self.role_directories_root)

    def create_role_directory(self, handle: RunHandle, *components: str) -> Path:
        """Create one private packet-only CWD without exposing a repository path."""
        self.assert_handle(handle)
        if not components or any(
            re.fullmatch(r"[a-z0-9][a-z0-9-]*", component) is None
            for component in components
        ):
            raise ValueError("role directory components violate the closed grammar")
        if self._flat_role_directories:
            assert self.role_directories_root is not None
            leaf = "-".join((".dialectic-role", handle.run_id, *components))
            current = self.role_directories_root / leaf
            if current.exists():
                raise FileExistsError(f"role directory already exists: {components[-1]}")
            os.mkdir(current, mode=0o700)
            _apply_private_directory_security(current)
            _verify_private_directory(current, current.parent)
            return current.resolve(strict=True)
        if self.role_directories_root is None:
            base = handle.path
        else:
            base = self.role_directories_root / handle.run_id
            _ensure_private_directory(base)
        current = base
        for index, component in enumerate(components):
            current = current / component
            final = index == len(components) - 1
            if current.exists():
                if final:
                    raise FileExistsError(f"role directory already exists: {component}")
                _verify_private_directory(current, current.parent)
                continue
            os.mkdir(current, mode=0o700)
            _apply_private_directory_security(current)
            _verify_private_directory(current, current.parent)
        return current.resolve(strict=True)

    @contextmanager
    def temporary_role_directory(self, *, prefix: str) -> Iterator[Path]:
        parent = self.role_directories_root or self.state_root
        with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as root:
            path = Path(root).resolve(strict=True)
            _apply_private_directory_security(path)
            _verify_private_directory(path, parent.resolve(strict=True))
            yield path

    def bootstrap_run(self, mode: RunMode) -> RunHandle:
        last_error: Exception | None = None
        for _ in range(3):
            run_id = validate_run_id(self._run_id_factory())
            suffix = self._suffix_factory()
            if not re.fullmatch(r"[0-9a-f]{12}", suffix):
                raise BootstrapError("bootstrap suffix generator violated its closed grammar")
            unpublished = self.runs_root / f".{run_id}.bootstrap-{suffix}"
            final = self.runs_root / run_id
            created_identity: tuple[int, int] | None = None
            try:
                os.mkdir(unpublished, mode=0o700)
                created_info = unpublished.lstat()
                created_identity = (created_info.st_dev, created_info.st_ino)
                _apply_private_directory_security(unpublished)
                self._privacy_verifier(unpublished, self.runs_root)
                created = _created_record(run_id, mode)
                _create_private_file(unpublished / "run.json", canonical_json_bytes(created))
                RunRecord.model_validate_json((unpublished / "run.json").read_bytes(), strict=True)
                self._publisher(unpublished, final)
                return RunHandle(run_id, final.resolve(strict=True), self._nonce)
            except (_PublishCollision, FileExistsError) as exc:
                last_error = exc
                if created_identity is not None:
                    _best_effort_bootstrap_cleanup(unpublished, created_identity)
                continue
            except Exception as exc:
                if created_identity is not None:
                    _best_effort_bootstrap_cleanup(unpublished, created_identity)
                raise BootstrapError(_bounded_bootstrap_message(exc)) from exc
        raise BootstrapError(
            _bounded_bootstrap_message(last_error or RuntimeError("run id collision bound exhausted"))
        )

    def assert_handle(self, handle: RunHandle) -> Path:
        if handle._store_nonce != self._nonce:
            raise PermissionError("run handle was not issued by this store")
        validate_run_id(handle.run_id)
        expected = (self.runs_root / handle.run_id).resolve(strict=True)
        if expected != handle.path:
            raise PermissionError("run handle path does not match its run id")
        return expected

    def read_run(self, run_id: str) -> RunRecord:
        validate_run_id(run_id)
        path = self.runs_root / run_id / "run.json"
        if not path.exists():
            raise RunNotFoundError(f"run not found: {run_id}")
        try:
            raw = _bounded_artifact_read(path, 1_048_576)
            record = RunRecord.model_validate_json(raw, strict=True)
        except (OSError, ValidationError, ValueError) as exc:
            raise StateCorruptError(f"run state is corrupt: {run_id}") from exc
        if record.run_id != run_id:
            raise StateCorruptError(f"run state id mismatch: {run_id}")
        return record

    def read_handle(self, handle: RunHandle) -> RunRecord:
        self.assert_handle(handle)
        return self.read_run(handle.run_id)

    def read_summary(self, run_id: str) -> SummaryRecord:
        validate_run_id(run_id)
        path = self.runs_root / run_id / "summary.json"
        if not path.exists():
            raise RunNotFoundError(f"run summary not found: {run_id}")
        try:
            raw = _bounded_artifact_read(path, 1_048_576)
            summary = SummaryRecord.model_validate_json(raw, strict=True)
        except (OSError, ValidationError, ValueError) as exc:
            raise StateCorruptError(f"run summary is corrupt: {run_id}") from exc
        if summary.run_id != run_id:
            raise StateCorruptError(f"run summary id mismatch: {run_id}")
        return summary

    def read_workspace(self, run_id: str) -> WorkspaceRecord | None:
        validate_run_id(run_id)
        path = self.runs_root / run_id / "git" / "workspace.json"
        if not path.exists():
            return None
        try:
            raw = _bounded_artifact_read(path, 1_048_576)
            return WorkspaceRecord.model_validate_json(raw, strict=True)
        except (OSError, ValidationError, ValueError) as exc:
            raise StateCorruptError(f"workspace state is corrupt: {run_id}") from exc

    def write_run(
        self,
        handle: RunHandle,
        record: RunRecord,
        *,
        replace_func: Callable[[str | bytes | os.PathLike[str], str | bytes | os.PathLike[str]], None]
        | None = None,
    ) -> None:
        run_dir = self.assert_handle(handle)
        if record.run_id != handle.run_id:
            raise ValueError("record run id does not match handle")
        previous = self.read_run(handle.run_id)
        _validate_transition(previous, record)
        _atomic_write_private(
            run_dir / "run.json",
            canonical_json_bytes(record),
            replace_func=replace_func or os.replace,
        )

    def append_event(self, handle: RunHandle, event: EventRecord) -> None:
        run_dir = self.assert_handle(handle)
        if event.run_id != handle.run_id:
            raise ValueError("event run id does not match handle")
        path = run_dir / "events.jsonl"
        expected = self._next_event_sequence(path)
        if event.sequence != expected:
            raise ValueError(f"event sequence must be {expected}")
        data = canonical_json_bytes(event)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _apply_private_file_security(path)

    def write_artifact(
        self,
        handle: RunHandle,
        relative_path: str,
        content: BaseModel | bytes,
        *,
        immutable: bool = True,
    ) -> str:
        run_dir = self.assert_handle(handle)
        path = _safe_artifact_path(run_dir, relative_path)
        data = canonical_json_bytes(content) if isinstance(content, BaseModel) else content
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _apply_private_directory_security(path.parent)
        if immutable:
            try:
                _create_private_file(path, data)
            except FileExistsError as exc:
                raise ArtifactExistsError(relative_path) from exc
        else:
            _atomic_write_private(path, data)
        return artifact_sha256(data)

    def read_artifact(self, handle: RunHandle, relative_path: str, limit: int) -> bytes:
        run_dir = self.assert_handle(handle)
        return _bounded_artifact_read(_safe_artifact_path(run_dir, relative_path), limit)

    def read_capability_attestation(self, cache_key: str) -> bytes | None:
        path = self._capability_attestation_path(cache_key)
        if not path.exists():
            return None
        return _bounded_artifact_read(path, 1_048_576)

    def write_capability_attestation(
        self,
        cache_key: str,
        artifact: CapabilityAttestationArtifact,
    ) -> str:
        data = canonical_json_bytes(artifact)
        _atomic_write_private(self._capability_attestation_path(cache_key), data)
        return artifact_sha256(data)

    def _capability_attestation_path(self, cache_key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ValueError("capability attestation cache key must be lowercase SHA-256")
        return self.capability_attestations_root / f"{cache_key}.json"

    @staticmethod
    def _next_event_sequence(path: Path) -> int:
        if not path.exists():
            return 1
        sequence = 0
        with path.open("rb") as stream:
            for line in stream:
                sequence += 1
                try:
                    event = EventRecord.model_validate_json(line, strict=True)
                except ValidationError as exc:
                    raise StateCorruptError("events.jsonl is corrupt") from exc
                if event.sequence != sequence:
                    raise StateCorruptError("events.jsonl sequence is not contiguous")
        return sequence + 1

    def next_event_sequence(self, handle: RunHandle) -> int:
        run_dir = self.assert_handle(handle)
        return self._next_event_sequence(run_dir / "events.jsonl")


def _created_record(run_id: str, mode: RunMode) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        tool_version=TOOL_VERSION,
        run_id=run_id,
        mode=mode,
        status="CREATED",
        phase=None,
        code_outcome=None,
        consensus_outcome=None,
        failure_kind=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
        started_model_work_at=None,
        completed_at=None,
    )


def _validate_transition(previous: RunRecord, current: RunRecord) -> None:
    if previous.mode != current.mode or previous.created_at != current.created_at:
        raise ValueError("immutable run identity fields changed")
    if current.updated_at < previous.updated_at:
        raise ValueError("run update time moved backwards")
    terminal = {"FINALIZED", "FAILED", "TIMED_OUT", "CANCELLED"}
    if previous.status in terminal:
        raise ValueError("terminal run records cannot transition")
    allowed = {
        "CREATED": {"RUNNING", "FAILED"},
        "RUNNING": {"RUNNING", *terminal},
    }
    if current.status not in allowed[previous.status]:
        raise ValueError(f"invalid run status transition {previous.status} -> {current.status}")
    if previous.phase is not None and current.phase is not None:
        phases = CODE_PHASES if current.mode == "code" else COUNCIL_PHASES
        if phases.index(current.phase) < phases.index(previous.phase):
            raise ValueError("backward phase transitions are disabled in version 0.1")


def _safe_artifact_path(run_dir: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError("artifact path must be a bounded relative path without traversal")
    path = run_dir.joinpath(candidate)
    if os.path.commonpath((str(run_dir), str(path.resolve(strict=False)))) != str(run_dir):
        raise ValueError("artifact path escapes the run directory")
    return path


def _atomic_write_private(
    path: Path,
    data: bytes,
    *,
    replace_func: Callable[[str | bytes | os.PathLike[str], str | bytes | os.PathLike[str]], None] = os.replace,
) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(6)}")
    try:
        _create_private_file(temporary, data)
        replace_func(temporary, path)
        _apply_private_file_security(path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _create_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("private artifact write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _apply_private_file_security(path)


def _bounded_artifact_read(path: Path, limit: int) -> bytes:
    with path.open("rb", buffering=0) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise StateCorruptError(f"artifact exceeds {limit} bytes")
    return data


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _apply_private_directory_security(path)


def ensure_private_directory(path: Path) -> None:
    """Create or re-secure a private artifact directory."""

    _ensure_private_directory(path)


def _apply_private_directory_security(path: Path) -> None:
    if os.name == "nt":
        _apply_windows_private_dacl(path, directory=True)
    else:
        os.chmod(path, 0o700, follow_symlinks=False)


def _apply_private_file_security(path: Path) -> None:
    if os.name == "nt":
        _apply_windows_private_dacl(path, directory=False)
    else:
        os.chmod(path, 0o600, follow_symlinks=False)


def apply_private_file_security(path: Path) -> None:
    """Apply the platform private-file policy to an existing artifact."""

    _apply_private_file_security(path)


def _apply_windows_private_dacl(path: Path, *, directory: bool) -> None:
    try:
        import ntsecuritycon
        import win32api
        import win32security
    except ImportError as exc:  # pragma: no cover - dependency failure is deterministic
        raise OSError("pywin32 is required to establish private Windows permissions") from exc

    user_name = win32api.GetUserNameEx(win32api.NameSamCompatible)
    user_sid, _, _ = win32security.LookupAccountName(None, user_name)
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    acl = win32security.ACL()
    inheritance = 0
    if directory:
        inheritance = ntsecuritycon.CONTAINER_INHERIT_ACE | ntsecuritycon.OBJECT_INHERIT_ACE
    for sid in (user_sid, system_sid):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        user_sid,
        None,
        acl,
        None,
    )


def _verify_private_directory(path: Path, parent: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise PermissionError("bootstrap sibling is not an ordinary directory")
    if path.parent.resolve(strict=True) != parent.resolve(strict=True):
        raise PermissionError("bootstrap sibling parent identity changed")
    if os.name == "nt":
        _verify_windows_private_dacl(path)
    else:
        if info.st_uid != os.geteuid():
            raise PermissionError("bootstrap sibling owner is not the current user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("bootstrap sibling permissions are not 0700")


def _verify_windows_private_dacl(path: Path) -> None:
    import win32api
    import win32security

    user_name = win32api.GetUserNameEx(win32api.NameSamCompatible)
    user_sid, _, _ = win32security.LookupAccountName(None, user_name)
    system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    if owner != user_sid:
        raise PermissionError("bootstrap sibling owner is not the current user")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise PermissionError("bootstrap sibling has no private DACL")
    permitted = {str(user_sid), str(system_sid)}
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if str(ace[2]) not in permitted:
            raise PermissionError("bootstrap sibling DACL grants an unexpected principal")


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file_ex = kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file_ex.restype = ctypes.c_int
        if not move_file_ex(str(source), str(destination), 0x8):  # MOVEFILE_WRITE_THROUGH
            code = ctypes.get_last_error()
            if code in {80, 183}:
                raise _PublishCollision(str(destination))
            raise ctypes.WinError(code)
        return

    if not sys.platform.startswith("linux"):
        raise OSError("atomic no-replace directory publication is unsupported on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError("renameat2 is required for atomic no-replace publication")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise _PublishCollision(str(destination))
        raise OSError(code, os.strerror(code), str(destination))


def _best_effort_bootstrap_cleanup(path: Path, expected_identity: tuple[int, int]) -> None:
    try:
        info = path.lstat()
        if (
            (info.st_dev, info.st_ino) != expected_identity
            or not stat.S_ISDIR(info.st_mode)
            or path.is_symlink()
        ):
            return
        with os.scandir(path) as entries:
            children = list(entries)
        if len(children) > 1:
            return
        if children:
            child = children[0]
            child_info = child.stat(follow_symlinks=False)
            if (
                child.name != "run.json"
                or not stat.S_ISREG(child_info.st_mode)
                or stat.S_ISLNK(child_info.st_mode)
            ):
                return
            os.unlink(child.path)
        path.rmdir()
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _bounded_bootstrap_message(exc: Exception) -> str:
    message = f"secure run bootstrap failed: {type(exc).__name__}"
    return message.encode("utf-8")[:4096].decode("utf-8", errors="ignore")


# Imported late to keep the closed phase tuples in one source module.
from .contracts import CODE_PHASES, COUNCIL_PHASES  # noqa: E402
