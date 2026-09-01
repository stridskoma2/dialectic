"""Concrete creation-time Job Object backend for native Windows turns."""

from __future__ import annotations

import ctypes
import math
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Mapping, Sequence

from .process import WindowsCreatedProcess

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x0000_2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x0002_0002
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002_000D
_STARTF_USESTDHANDLES = 0x0000_0100
_HANDLE_FLAG_INHERIT = 0x0000_0001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFF_FFFF
_CREATE_NEW_PROCESS_GROUP = 0x0000_0200
_CREATE_NO_WINDOW = 0x0800_0000
_CTRL_BREAK_EVENT = 1
_ERROR_BROKEN_PIPE = 109


@dataclass(slots=True)
class _WinHandle:
    value: int
    name: str
    process_id: int | None = None
    closed: bool = False


@dataclass(slots=True)
class _AttributeList:
    buffer: object
    inherited_handles: tuple[_WinHandle, ...]
    keepalive: tuple[object, ...]
    closed: bool = False

    @property
    def pointer(self) -> wintypes.LPVOID:
        return ctypes.cast(self.buffer, wintypes.LPVOID)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


class CtypesWindowsJobBackend:
    """ctypes/pywin32 implementation of ``WindowsJobBackend``."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 Job execution is available only on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def nested_jobs_supported(self) -> bool:
        version = sys.getwindowsversion()
        return (version.major, version.minor) >= (6, 2)

    def create_kill_on_close_job(self) -> object:
        raw = self._kernel32.CreateJobObjectW(None, None)
        if not raw:
            raise ctypes.WinError(ctypes.get_last_error())
        job = _WinHandle(_raw_handle(raw), "job")
        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            wintypes.HANDLE(job.value), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            self.close_resource(job)
            raise ctypes.WinError(ctypes.get_last_error())
        return job

    def create_standard_stream_pipes(self) -> Mapping[str, tuple[object, object]]:
        return {
            "stdin": self._create_pipe("stdin", parent_reads=False),
            "stdout": self._create_pipe("stdout", parent_reads=True),
            "stderr": self._create_pipe("stderr", parent_reads=True),
        }

    def create_attribute_list(
        self, *, job: object, inherited_handles: Sequence[object]
    ) -> object:
        job_handle = self._require_handle(job)
        handles = tuple(self._require_handle(item) for item in inherited_handles)
        size = ctypes.c_size_t()
        self._kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        pointer = ctypes.cast(buffer, wintypes.LPVOID)
        if not self._kernel32.InitializeProcThreadAttributeList(
            pointer, 2, 0, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        job_array = (wintypes.HANDLE * 1)(job_handle.value)
        handle_array = (wintypes.HANDLE * len(handles))(*(item.value for item in handles))
        try:
            self._update_attribute(
                pointer, _PROC_THREAD_ATTRIBUTE_JOB_LIST,
                ctypes.cast(job_array, wintypes.LPVOID), ctypes.sizeof(job_array),
            )
            self._update_attribute(
                pointer, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(handle_array, wintypes.LPVOID), ctypes.sizeof(handle_array),
            )
        except Exception:
            self._kernel32.DeleteProcThreadAttributeList(pointer)
            raise
        return _AttributeList(buffer, handles, (job_array, handle_array))

    def create_process_suspended(
        self, *, executable: str, arguments: Sequence[str], cwd: str,
        environment: Mapping[str, str], attribute_list: object, creation_flags: int,
    ) -> WindowsCreatedProcess:
        attributes = self._require_attributes(attribute_list)
        if len(attributes.inherited_handles) != 3:
            raise RuntimeError("native launch requires stdin, stdout, and stderr handles")
        stdin_handle, stdout_handle, stderr_handle = attributes.inherited_handles
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_handle.value
        startup.StartupInfo.hStdOutput = stdout_handle.value
        startup.StartupInfo.hStdError = stderr_handle.value
        startup.lpAttributeList = attributes.pointer
        info = _PROCESS_INFORMATION()
        command = ctypes.create_unicode_buffer(
            subprocess.list2cmdline((executable, *arguments))
        )
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(
                f"{name}={value}" for name, value in sorted(
                    environment.items(), key=lambda item: item[0].casefold()
                )
            ) + "\0\0"
        )
        if not self._kernel32.CreateProcessW(
            executable, command, None, None, True,
            creation_flags | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            environment_block, cwd, ctypes.byref(startup), ctypes.byref(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process_id = int(info.dwProcessId)
        return WindowsCreatedProcess(
            _WinHandle(_raw_handle(info.hProcess), "process", process_id),
            _WinHandle(_raw_handle(info.hThread), "thread"),
            process_id,
        )

    def verify_job_membership(self, process: object, job: object) -> bool:
        result = wintypes.BOOL()
        if not self._kernel32.IsProcessInJob(
            self._require_handle(process).value, self._require_handle(job).value,
            ctypes.byref(result),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(result.value)

    def resume_thread(self, thread: object) -> None:
        if self._kernel32.ResumeThread(self._require_handle(thread).value) == 0xFFFF_FFFF:
            raise ctypes.WinError(ctypes.get_last_error())

    def request_graceful_termination(self, process: object) -> bool:
        handle = self._require_handle(process)
        return bool(
            handle.process_id is not None
            and self._kernel32.GenerateConsoleCtrlEvent(
                _CTRL_BREAK_EVENT, handle.process_id
            )
        )

    def terminate_job(self, job: object) -> None:
        if not self._kernel32.TerminateJobObject(self._require_handle(job).value, 1):
            error = ctypes.get_last_error()
            if error not in {0, 5}:
                raise ctypes.WinError(error)

    def wait_process(self, process: object, timeout_seconds: float | None) -> int | None:
        milliseconds = _INFINITE if timeout_seconds is None else min(
            _INFINITE - 1, max(0, math.ceil(timeout_seconds * 1000))
        )
        handle = self._require_handle(process)
        wait = self._kernel32.WaitForSingleObject(handle.value, milliseconds)
        if wait == _WAIT_TIMEOUT:
            return None
        if wait != _WAIT_OBJECT_0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(handle.value, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(exit_code.value)

    def read_pipe(self, pipe: object, maximum_bytes: int) -> bytes:
        import pywintypes
        import win32file

        try:
            _, data = win32file.ReadFile(self._require_handle(pipe).value, maximum_bytes)
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_BROKEN_PIPE:
                return b""
            raise
        return bytes(data)

    def write_pipe(self, pipe: object, data: bytes) -> None:
        import win32file

        handle = self._require_handle(pipe)
        offset = 0
        while offset < len(data):
            _, result = win32file.WriteFile(handle.value, data[offset:])
            count = int(result) if isinstance(result, int) else len(result)
            if count <= 0:
                raise OSError("Win32 pipe write made no progress")
            offset += count

    def close_resource(self, resource: object) -> None:
        if isinstance(resource, _AttributeList):
            if not resource.closed:
                self._kernel32.DeleteProcThreadAttributeList(resource.pointer)
                resource.closed = True
            return
        handle = self._require_handle(resource)
        if not self._kernel32.CloseHandle(handle.value):
            raise ctypes.WinError(ctypes.get_last_error())
        handle.closed = True

    @staticmethod
    def resource_is_closed(resource: object) -> bool:
        if isinstance(resource, (_WinHandle, _AttributeList)):
            return resource.closed
        raise TypeError("unexpected Win32 resource")

    def _create_pipe(self, name: str, *, parent_reads: bool) -> tuple[object, object]:
        security = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        read_raw = wintypes.HANDLE()
        write_raw = wintypes.HANDLE()
        if not self._kernel32.CreatePipe(
            ctypes.byref(read_raw), ctypes.byref(write_raw), ctypes.byref(security), 0
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        read = _WinHandle(_raw_handle(read_raw), f"{name}-read")
        write = _WinHandle(_raw_handle(write_raw), f"{name}-write")
        parent, child = (read, write) if parent_reads else (write, read)
        if not self._kernel32.SetHandleInformation(
            parent.value, _HANDLE_FLAG_INHERIT, 0
        ):
            self.close_resource(read)
            self.close_resource(write)
            raise ctypes.WinError(ctypes.get_last_error())
        return parent, child

    def _update_attribute(
        self, pointer: wintypes.LPVOID, attribute: int,
        value: wintypes.LPVOID, size: int,
    ) -> None:
        if not self._kernel32.UpdateProcThreadAttribute(
            pointer, 0, attribute, value, size, None, None
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _require_handle(resource: object) -> _WinHandle:
        if not isinstance(resource, _WinHandle) or resource.closed:
            raise TypeError("expected an open Win32 handle")
        return resource

    @staticmethod
    def _require_attributes(resource: object) -> _AttributeList:
        if not isinstance(resource, _AttributeList) or resource.closed:
            raise TypeError("expected an open Win32 attribute list")
        return resource

    def _configure_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.DWORD,
        ]
        kernel32.CreatePipe.restype = wintypes.BOOL
        kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            wintypes.LPVOID,
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL


def _raw_handle(value: object) -> int:
    if isinstance(value, int):
        return value
    raw = getattr(value, "value", None)
    if raw is None:
        raise TypeError("Win32 call returned an invalid handle")
    return int(raw)
