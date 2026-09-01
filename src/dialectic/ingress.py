"""Ceiling-plus-one, regular-file-only acquisition for CLI-named inputs."""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path

from .contracts import MAX_DIAGNOSTIC_BYTES, MAX_NAMED_INPUT_BYTES


class InputAcquisitionError(ValueError):
    def __init__(self, diagnostic: str) -> None:
        bounded = diagnostic.encode("utf-8")[:MAX_DIAGNOSTIC_BYTES]
        super().__init__(bounded.decode("utf-8", errors="ignore"))


def acquire_named_file(
    path: Path | str,
    *,
    label: str,
    ceiling: int = MAX_NAMED_INPUT_BYTES,
) -> bytes:
    if not 0 < ceiling <= MAX_NAMED_INPUT_BYTES:
        raise ValueError("named input ceiling is outside the product bound")
    selected = os.fspath(path)
    if _is_device_namespace(selected):
        raise InputAcquisitionError(f"{label} path uses a rejected device namespace")
    try:
        return (
            _acquire_windows(Path(selected), label, ceiling)
            if os.name == "nt"
            else _acquire_posix(Path(selected), label, ceiling)
        )
    except InputAcquisitionError:
        raise
    except OSError as exc:
        raise InputAcquisitionError(f"unable to acquire {label}: {type(exc).__name__}") from exc


def _acquire_posix(path: Path, label: str, ceiling: int) -> bytes:
    # Some kernels reject opening socket paths before fstat can classify them.
    # This check provides the required bounded diagnostic; the opened handle and
    # its before/after metadata remain authoritative against replacement races.
    selected = path.lstat()
    if not stat.S_ISREG(selected.st_mode):
        raise InputAcquisitionError(f"{label} must be a regular file")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise InputAcquisitionError(f"{label} must be a regular file")
        if before.st_size > ceiling:
            raise InputAcquisitionError(f"{label} exceeds the {ceiling}-byte ceiling")
        data = _read_fd_bounded(fd, ceiling + 1)
        after = os.fstat(fd)
        before_identity = (before.st_dev, before.st_ino)
        after_identity = (after.st_dev, after.st_ino)
        if before_identity != after_identity or before.st_size != after.st_size:
            raise InputAcquisitionError(f"{label} identity or size changed during acquisition")
        if len(data) > ceiling:
            raise InputAcquisitionError(f"{label} exceeds the {ceiling}-byte ceiling")
        if len(data) != after.st_size:
            raise InputAcquisitionError(f"{label} did not yield its stable declared size")
        return data
    finally:
        os.close(fd)


def _read_fd_bounded(fd: int, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = os.read(fd, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _acquire_windows(path: Path, label: str, ceiling: int) -> bytes:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    kernel32.GetFileType.argtypes = [ctypes.c_void_p]
    kernel32.GetFileType.restype = ctypes.c_uint32
    kernel32.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x1 | 0x2 | 0x4,  # share read/write/delete so stability checks remain meaningful
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x02000000 | 0x08000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS | SEQUENTIAL_SCAN
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        before = _windows_file_info(kernel32, handle)
        attributes, size, identity = before
        if kernel32.GetFileType(handle) != 0x1:  # FILE_TYPE_DISK
            raise InputAcquisitionError(f"{label} must be a regular disk file")
        if attributes & (0x10 | 0x400):  # DIRECTORY | REPARSE_POINT
            raise InputAcquisitionError(f"{label} must be a non-reparse regular file")
        if size > ceiling:
            raise InputAcquisitionError(f"{label} exceeds the {ceiling}-byte ceiling")
        data = _read_windows_handle(kernel32, handle, ceiling + 1)
        after_attributes, after_size, after_identity = _windows_file_info(kernel32, handle)
        if attributes != after_attributes or identity != after_identity or size != after_size:
            raise InputAcquisitionError(f"{label} identity or size changed during acquisition")
        if len(data) > ceiling:
            raise InputAcquisitionError(f"{label} exceeds the {ceiling}-byte ceiling")
        if len(data) != after_size:
            raise InputAcquisitionError(f"{label} did not yield its stable declared size")
        return data
    finally:
        kernel32.CloseHandle(handle)


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTimeLow", ctypes.c_uint32),
        ("ftCreationTimeHigh", ctypes.c_uint32),
        ("ftLastAccessTimeLow", ctypes.c_uint32),
        ("ftLastAccessTimeHigh", ctypes.c_uint32),
        ("ftLastWriteTimeLow", ctypes.c_uint32),
        ("ftLastWriteTimeHigh", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


def _windows_file_info(kernel32: object, handle: int) -> tuple[int, int, tuple[int, int]]:
    info = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    size = (info.nFileSizeHigh << 32) | info.nFileSizeLow
    file_id = (info.nFileIndexHigh << 32) | info.nFileIndexLow
    return info.dwFileAttributes, size, (info.dwVolumeSerialNumber, file_id)


def _read_windows_handle(kernel32: object, handle: int, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        requested = min(65_536, remaining)
        buffer = ctypes.create_string_buffer(requested)
        read = ctypes.c_uint32()
        if not kernel32.ReadFile(handle, buffer, requested, ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if read.value == 0:
            break
        chunks.append(buffer.raw[: read.value])
        remaining -= read.value
    return b"".join(chunks)


def _is_device_namespace(path: str) -> bool:
    normalized = path.replace("/", "\\")
    upper = normalized.upper()
    if upper.startswith(("\\\\.\\", "\\\\?\\", "\\??\\")):
        return True
    if os.name != "nt":
        return False
    basename = Path(path).name.rstrip(" .").split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    return basename in reserved
