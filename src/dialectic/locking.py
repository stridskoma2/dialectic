"""Stable-filesystem-identity advisory repository locking."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout


class RepositoryIdentityError(RuntimeError):
    pass


class RepositoryBusyError(RuntimeError):
    def __init__(self, holding_run_id: str | None) -> None:
        self.holding_run_id = holding_run_id
        detail = f"; holding run {holding_run_id}" if holding_run_id else ""
        super().__init__(f"repository advisory lock is held{detail}")


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    canonical_path: Path
    filesystem_identity: str
    lock_identity_sha256: str


def resolve_repository_identity(path: Path | str) -> RepositoryIdentity:
    try:
        canonical = Path(path).resolve(strict=True)
        info = canonical.stat()
    except OSError as exc:
        raise RepositoryIdentityError("repository identity cannot be established") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RepositoryIdentityError("repository common path is not a directory")
    platform_tag = "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else sys.platform
    filesystem_identity = f"{info.st_dev:x}:{info.st_ino:x}"
    digest = hashlib.sha256(
        platform_tag.encode("ascii") + filesystem_identity.encode("ascii")
    ).hexdigest()
    return RepositoryIdentity(canonical, filesystem_identity, digest)


class RepositoryLock:
    def __init__(
        self,
        locks_root: Path | str,
        identity: RepositoryIdentity,
        run_id: str,
    ) -> None:
        self._root = Path(locks_root)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self._root, 0o700)
        self.identity = identity
        self.run_id = run_id
        self.lock_path = self._root / f"{identity.lock_identity_sha256}.lock"
        self.sidecar_path = self._root / f"{identity.lock_identity_sha256}.meta"
        self._lock = FileLock(self.lock_path)
        self._held = False

    def acquire(self) -> "RepositoryLock":
        try:
            self._lock.acquire(timeout=0)
        except Timeout as exc:
            raise RepositoryBusyError(self._read_holding_run()) from exc
        try:
            self._write_sidecar()
        except Exception:
            self._lock.release()
            raise
        self._held = True
        return self

    def release(self) -> None:
        if not self._held:
            return
        try:
            try:
                if self._read_holding_run() == self.run_id:
                    self.sidecar_path.unlink(missing_ok=True)
            finally:
                self._lock.release()
        finally:
            self._held = False

    def __enter__(self) -> "RepositoryLock":
        return self.acquire()

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()

    def _write_sidecar(self) -> None:
        # Text avoids creating a schema-unbound controller JSON object.
        data = f"run_id={self.run_id}\npath={self.identity.canonical_path}\n".encode("utf-8")
        temporary = self.sidecar_path.with_name(
            f".{self.sidecar_path.name}.tmp-{secrets.token_hex(6)}"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.sidecar_path)
        if os.name != "nt":
            os.chmod(self.sidecar_path, 0o600)

    def _read_holding_run(self) -> str | None:
        try:
            with self.sidecar_path.open("rt", encoding="utf-8", errors="strict") as stream:
                first = stream.readline(256)
        except (OSError, UnicodeError):
            return None
        if not first.startswith("run_id="):
            return None
        value = first.removeprefix("run_id=").strip()
        return value or None
