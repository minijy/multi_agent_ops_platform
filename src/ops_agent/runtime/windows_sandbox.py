from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]


class WindowsSandboxError(RuntimeError):
    pass


WINDOWS_SAFE_ENV_NAMES = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "USERNAME",
    "USERDOMAIN",
    "HOMEDRIVE",
    "HOMEPATH",
    "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "LOCALAPPDATA",
    "LANG",
    "LC_ALL",
)

PROFILE_READ_ONLY = "opsagentsandboxro"
PROFILE_WORKSPACE_WRITE = "opsagentsandboxrw"


@dataclass(frozen=True)
class WindowsProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def windows_safe_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if source is None else source
    selected = {
        name: env[name]
        for name in WINDOWS_SAFE_ENV_NAMES
        if name in env and env[name]
    }
    system_root = selected.get("SYSTEMROOT") or selected.get("WINDIR")
    if system_root and "COMSPEC" not in selected:
        comspec = str(Path(system_root) / "System32" / "cmd.exe")
        if Path(comspec).is_file():
            selected["COMSPEC"] = comspec
    return selected


def write_roots_for_mode(mode: SandboxMode, workspace_root: Path) -> list[Path]:
    if mode != "workspace-write":
        return []
    roots = {workspace_root.resolve()}
    for name in ("TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            roots.add(Path(value).expanduser().resolve())
    return sorted(roots)


def resolve_windows_executable(command0: str, env: Mapping[str, str]) -> str:
    candidate = Path(command0)
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(command0, path=env.get("PATH"))
    if found:
        return found
    system_root = env.get("SYSTEMROOT") or env.get("WINDIR") or r"C:\Windows"
    fallback = Path(system_root) / "System32" / command0
    if fallback.is_file():
        return str(fallback)
    if not command0.lower().endswith(".exe"):
        exe = fallback.with_name(fallback.name + ".exe")
        if exe.is_file():
            return str(exe)
    raise FileNotFoundError(f"windows sandbox executable not found: {command0}")


class WindowsAppContainerBackend:
    """Launch argv inside a capability-less AppContainer (no network by default)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WindowsSandboxError("Windows AppContainer backend requires win32")
        self._sids: dict[str, object] = {}
        self._ensure_profile(PROFILE_READ_ONLY)
        self._ensure_profile(PROFILE_WORKSPACE_WRITE)

    def probe(self) -> bool:
        env = windows_safe_env()
        try:
            exe = resolve_windows_executable("cmd.exe", env)
            cwd = Path(env.get("TEMP") or env.get("TMP") or ".")
            result = self.spawn(
                [exe, "/c", "echo", "opsagent-sandbox-probe"],
                mode="read-only",
                cwd=cwd,
                env=env,
                timeout_seconds=8,
                max_output_bytes=4096,
                write_roots=[],
            )
        except Exception:
            return False
        return result.exit_code == 0 and b"opsagent-sandbox-probe" in result.stdout

    def spawn(
        self,
        command: list[str],
        *,
        mode: SandboxMode,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
        write_roots: list[Path],
    ) -> WindowsProcessResult:
        profile = (
            PROFILE_WORKSPACE_WRITE if mode == "workspace-write" else PROFILE_READ_ONLY
        )
        sid = self._sids.get(profile) or self._ensure_profile(profile)
        if mode == "workspace-write":
            for root in write_roots:
                root.mkdir(parents=True, exist_ok=True)
                _grant_appcontainer_modify(sid, root)
        resolved = [resolve_windows_executable(command[0], env), *command[1:]]
        return _create_appcontainer_process(
            command=resolved,
            cwd=cwd,
            env=env,
            sid=sid,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

    def _ensure_profile(self, name: str):
        cached = self._sids.get(name)
        if cached is not None:
            return cached
        sid = _create_or_derive_profile_sid(name)
        self._sids[name] = sid
        return sid


def probe_windows_appcontainer() -> WindowsAppContainerBackend | None:
    if sys.platform != "win32":
        return None
    try:
        backend = WindowsAppContainerBackend()
    except Exception:
        return None
    return backend if backend.probe() else None


def _win_api():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32, userenv, advapi32


def _create_or_derive_profile_sid(name: str):
    ctypes, wintypes, _kernel32, userenv, _advapi32 = _win_api()
    sid = wintypes.LPVOID()
    create = userenv.CreateAppContainerProfile
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    create.restype = ctypes.HRESULT
    status = create(name, "Ops Agent Sandbox", "Restricted command sandbox", None, 0, ctypes.byref(sid))
    already_exists = status & 0xFFFFFFFF == 0x800700B7
    if status == 0 and sid.value:
        return sid
    if already_exists or not sid.value:
        derive = userenv.DeriveAppContainerSidFromAppContainerName
        derive.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID)]
        derive.restype = ctypes.HRESULT
        sid = wintypes.LPVOID()
        derived = derive(name, ctypes.byref(sid))
        if derived != 0 or not sid.value:
            raise WindowsSandboxError(
                f"failed to create AppContainer profile {name}: 0x{status & 0xFFFFFFFF:08X}"
            )
        return sid
    raise WindowsSandboxError(
        f"failed to create AppContainer profile {name}: 0x{status & 0xFFFFFFFF:08X}"
    )


def _grant_appcontainer_modify(sid, path: Path) -> None:
    ctypes, wintypes, _kernel32, _userenv, advapi32 = _win_api()

    class TRUSTEE(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", wintypes.LPVOID),
            ("MultipleTrusteeOperation", wintypes.DWORD),
            ("TrusteeForm", wintypes.DWORD),
            ("TrusteeType", wintypes.DWORD),
            ("ptstrName", wintypes.LPVOID),
        ]

    class EXPLICIT_ACCESS(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", wintypes.DWORD),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", TRUSTEE),
        ]

    GRANT_ACCESS = 1
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_WELL_KNOWN_GROUP = 5
    SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x4
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    GENERIC_EXECUTE = 0x20000000
    DELETE = 0x00010000

    access = EXPLICIT_ACCESS()
    access.grfAccessPermissions = GENERIC_READ | GENERIC_WRITE | GENERIC_EXECUTE | DELETE
    access.grfAccessMode = GRANT_ACCESS
    access.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
    access.Trustee.pMultipleTrustee = None
    access.Trustee.MultipleTrusteeOperation = 0
    access.Trustee.TrusteeForm = TRUSTEE_IS_SID
    access.Trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP
    access.Trustee.ptstrName = sid.value if hasattr(sid, "value") else sid

    dacl = wintypes.LPVOID()
    owner = wintypes.LPVOID()
    group = wintypes.LPVOID()
    sacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    get_info = advapi32.GetNamedSecurityInfoW
    get_info.restype = wintypes.DWORD
    status = get_info(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(dacl),
        ctypes.byref(sacl),
        ctypes.byref(descriptor),
    )
    if status != 0:
        raise WindowsSandboxError(f"failed to read ACL for {path}: {status}")

    new_dacl = wintypes.LPVOID()
    set_entries = advapi32.SetEntriesInAclW
    set_entries.restype = wintypes.DWORD
    status = set_entries(1, ctypes.byref(access), dacl, ctypes.byref(new_dacl))
    if status != 0:
        advapi32.LocalFree(descriptor)
        raise WindowsSandboxError(f"failed to build ACL for {path}: {status}")

    set_info = advapi32.SetNamedSecurityInfoW
    set_info.restype = wintypes.DWORD
    status = set_info(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        new_dacl,
        None,
    )
    advapi32.LocalFree(new_dacl)
    advapi32.LocalFree(descriptor)
    if status != 0:
        raise WindowsSandboxError(f"failed to grant AppContainer write access on {path}: {status}")


def _create_appcontainer_process(
    *,
    command: list[str],
    cwd: Path,
    env: Mapping[str, str],
    sid,
    timeout_seconds: float,
    max_output_bytes: int,
) -> WindowsProcessResult:
    ctypes, wintypes, kernel32, _userenv, _advapi32 = _win_api()

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", wintypes.LPVOID),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", wintypes.LPVOID),
            ("Capabilities", wintypes.LPVOID),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_SUSPENDED = 0x00000004
    CREATE_NO_WINDOW = 0x08000000
    STARTF_USESTDHANDLES = 0x00000100
    STARTF_USESHOWWINDOW = 0x00000001
    HANDLE_FLAG_INHERIT = 0x00000001
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_LIMIT_PROCESS_TIME = 0x0002
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    def make_pipe():
        security = SECURITY_ATTRIBUTES()
        security.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        security.bInheritHandle = True
        read = wintypes.HANDLE()
        write = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(security), 0):
            raise OSError("CreatePipe failed")
        if not kernel32.SetHandleInformation(read, HANDLE_FLAG_INHERIT, 0):
            raise OSError("SetHandleInformation failed")
        return read, write

    def read_handle(handle) -> bytes:
        chunks: list[bytes] = []
        total = 0
        buffer = ctypes.create_string_buffer(4096)
        read = wintypes.DWORD()
        while True:
            ok = kernel32.ReadFile(handle, buffer, 4096, ctypes.byref(read), None)
            if not ok or read.value == 0:
                break
            chunk = buffer.raw[: read.value]
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_output_bytes * 2:
                break
        return b"".join(chunks)

    stdout_read, stdout_write = make_pipe()
    stderr_read, stderr_write = make_pipe()

    size = ctypes.c_size_t()
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attribute_list = ctypes.create_string_buffer(size.value)
    if not kernel32.InitializeProcThreadAttributeList(attribute_list, 1, 0, ctypes.byref(size)):
        raise WindowsSandboxError("InitializeProcThreadAttributeList failed")

    capabilities = SECURITY_CAPABILITIES()
    capabilities.AppContainerSid = sid.value if hasattr(sid, "value") else sid
    capabilities.Capabilities = None
    capabilities.CapabilityCount = 0
    if not kernel32.UpdateProcThreadAttribute(
        attribute_list,
        0,
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        ctypes.byref(capabilities),
        ctypes.sizeof(capabilities),
        None,
        None,
    ):
        kernel32.DeleteProcThreadAttributeList(attribute_list)
        raise WindowsSandboxError("failed to attach AppContainer SID to process")

    startup = STARTUPINFOEXW()
    startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
    startup.StartupInfo.hStdOutput = stdout_write
    startup.StartupInfo.hStdError = stderr_write
    startup.StartupInfo.hStdInput = kernel32.GetStdHandle(-10)
    startup.lpAttributeList = ctypes.cast(attribute_list, wintypes.LPVOID)

    process_info = PROCESS_INFORMATION()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    env_block = ctypes.create_unicode_buffer(
        "\0".join(f"{key}={value}" for key, value in env.items()) + "\0"
    )
    created = kernel32.CreateProcessW(
        command[0],
        command_line,
        None,
        None,
        True,
        EXTENDED_STARTUPINFO_PRESENT
        | CREATE_UNICODE_ENVIRONMENT
        | CREATE_SUSPENDED
        | CREATE_NO_WINDOW,
        env_block,
        str(cwd),
        ctypes.byref(startup),
        ctypes.byref(process_info),
    )
    kernel32.CloseHandle(stdout_write)
    kernel32.CloseHandle(stderr_write)
    kernel32.DeleteProcThreadAttributeList(attribute_list)
    if not created:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(stdout_read)
        kernel32.CloseHandle(stderr_read)
        raise WindowsSandboxError(f"CreateProcessW into AppContainer failed: {error}")

    job = kernel32.CreateJobObjectW(None, None)
    if job:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_PROCESS_TIME
        )
        limits.BasicLimitInformation.PerProcessUserTimeLimit = int(
            max(1.0, timeout_seconds + 1.0) * 10_000_000
        )
        kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        kernel32.AssignProcessToJobObject(job, process_info.hProcess)

    kernel32.ResumeThread(process_info.hThread)
    kernel32.CloseHandle(process_info.hThread)

    stdout_holder: list[bytes] = [b""]
    stderr_holder: list[bytes] = [b""]

    def capture(handle, bucket):
        try:
            bucket[0] = read_handle(handle)
        finally:
            kernel32.CloseHandle(handle)

    stdout_thread = threading.Thread(
        target=capture, args=(stdout_read, stdout_holder), daemon=True
    )
    stderr_thread = threading.Thread(
        target=capture, args=(stderr_read, stderr_holder), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    timeout_ms = max(1, int(timeout_seconds * 1000))
    wait = kernel32.WaitForSingleObject(process_info.hProcess, timeout_ms)
    timed_out = wait == WAIT_TIMEOUT
    if timed_out:
        if job:
            kernel32.TerminateJobObject(job, 1)
        else:
            kernel32.TerminateProcess(process_info.hProcess, 1)
        kernel32.WaitForSingleObject(process_info.hProcess, INFINITE)

    exit_code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code))
    kernel32.CloseHandle(process_info.hProcess)
    if job:
        kernel32.CloseHandle(job)
    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    return WindowsProcessResult(
        exit_code=-1 if timed_out else int(exit_code.value),
        stdout=stdout_holder[0],
        stderr=stderr_holder[0],
        timed_out=timed_out,
    )
