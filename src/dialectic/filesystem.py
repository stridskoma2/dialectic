"""No-follow filesystem metadata needed by safety-sensitive validators."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path


def hard_link_count(path: Path) -> int:
    """Return the authoritative hard-link count for an already selected leaf."""

    if os.name != "nt":
        return path.lstat().st_nlink
    return _windows_file_information(path).nNumberOfLinks


def stable_filesystem_identity(path: Path) -> str:
    """Return a platform-stable identity for an existing filesystem object."""

    if os.name != "nt":
        information = path.lstat()
        return f"{information.st_dev:x}:{information.st_ino:x}"
    information = _windows_file_information(path)
    file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return f"{information.dwVolumeSerialNumber:x}:{file_index:x}"


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _windows_file_information(path: Path) -> _ByHandleFileInformation:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x1 | 0x2 | 0x4,  # share read, write, and delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _ByHandleFileInformation()
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
        get_information.restype = wintypes.BOOL
        if not get_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        return information
    finally:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
