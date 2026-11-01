"""Read-only Windows tokens and process identities. No elevation or PID killing."""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path


def elevation_report():
    if os.name != "nt":
        return {"status": "unsupported", "elevated": None}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        return {"status": "unknown", "elevated": None, "error": ctypes.get_last_error()}
    try:
        elevated, size = w.DWORD(), w.DWORD()
        if not advapi.GetTokenInformation(token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size)):
            return {"status": "unknown", "elevated": None, "error": ctypes.get_last_error()}
        return {"status": "observed", "elevated": bool(elevated.value)}
    finally:
        kernel.CloseHandle(token)


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]


def observation_api():
    if os.name != "nt":
        raise OSError("Windows required for process observations")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in [
        ("CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE),
        ("Process32FirstW", [w.HANDLE, ctypes.POINTER(PROCESSENTRY32)], w.BOOL),
        ("Process32NextW", [w.HANDLE, ctypes.POINTER(PROCESSENTRY32)], w.BOOL),
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        ("GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        ("IsProcessInJob", [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
        ("CloseHandle", [w.HANDLE], w.BOOL),
    ]:
        getattr(api, name).argtypes = args
        getattr(api, name).restype = result
    return api


def process_identity(api, handle, pid, job=None):
    image, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
    if not api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    created, exited, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
    if not api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    row = {"pid": pid, "image": image.value,
           "creation_ticks": (created.dwHighDateTime << 32) | created.dwLowDateTime}
    if job is not None:
        owned = w.BOOL()
        if not api.IsProcessInJob(handle, job, ctypes.byref(owned)):
            raise ctypes.WinError(ctypes.get_last_error())
        row["in_owned_job"] = bool(owned.value)
    return row


def live_process_identity(api, handle, pid, job=None):
    """Resolve exit races on the same retained handle, never on a reused PID."""
    waited = api.WaitForSingleObject(handle, 0)
    if waited == 0:
        return None
    if waited != 0x102:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return process_identity(api, handle, pid, job)
    except OSError:
        # A process can exit between the wait and QueryFullProcessImageNameW.
        # Access denied alone is not evidence of exit; the handle must signal.
        # Teardown can revoke image-query access before the process object
        # signals. Wait on this candidate's handle, not the assistant root:
        # the game may exit/restart while the assistant remains alive.
        waited = api.WaitForSingleObject(handle, 2000)
        if waited == 0:
            return None
        if waited == 0x102:
            # A short-lived query denial can also recover without exit. A
            # fresh identity on the same handle still verifies the process;
            # persistent denial remains an error and blocks observation.
            return process_identity(api, handle, pid, job)
        raise


def snapshot_pids(api):
    """A fresh successful snapshot can confirm a stale candidate is gone."""
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        pids = set()
        found = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            pids.add(entry.th32ProcessID)
            found = api.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ctypes.WinError(ctypes.get_last_error())
        return pids
    finally:
        api.CloseHandle(snapshot)


def open_snapshot_process(api, pid):
    """Don't mistake a Toolhelp-to-OpenProcess exit race for access denial.

    No handle means we cannot identify or adopt the process. Only invalid PID
    plus absence from a new complete snapshot permits treating it as gone.
    A still-listed/reused PID, denied access or failed snapshot remains unknown.
    """
    handle = api.OpenProcess(0x101000, False, pid)
    if handle:
        return handle
    error = ctypes.get_last_error()
    if error == 87 and pid not in snapshot_pids(api):
        return None
    raise ctypes.WinError(error)


class ProcessObservationError(OSError):
    """Exclusivity could not be observed, distinct from a missing config file."""


def inspect_process_identity(pid):
    api = observation_api()
    handle = open_snapshot_process(api, pid)
    try:
        return live_process_identity(api, handle, pid) if handle is not None else None
    finally:
        if handle is not None:
            api.CloseHandle(handle)


def find_matching_processes(paths):
    try:
        return _find_matching_processes(paths)
    except OSError as exc:
        raise ProcessObservationError(f'process_observation_unconfirmed: {exc}') from exc


def _find_matching_processes(paths):
    """Only query matching basenames; unreadable candidates block exclusivity."""
    if os.name != "nt":
        return {"status": "unsupported", "matches": [], "uncertain": []}
    wanted = {os.path.normcase(str(Path(path).resolve())) for path in paths if path}
    names = {Path(path).name.casefold() for path in wanted}
    api = observation_api()
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    matches, uncertain = [], []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        found = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.casefold() in names:
                try:
                    handle = open_snapshot_process(api, entry.th32ProcessID)
                except OSError as exc:
                    uncertain.append({"pid": entry.th32ProcessID, "name": entry.szExeFile,
                                      "error": getattr(exc, 'winerror', None) or str(exc)})
                else:
                    if handle is None:
                        found = api.Process32NextW(snapshot, ctypes.byref(entry))
                        continue
                    try:
                        row = live_process_identity(api, handle, entry.th32ProcessID)
                        if row is not None and os.path.normcase(row["image"]) in wanted:
                            matches.append(row)
                    except OSError as exc:
                        uncertain.append({"pid": entry.th32ProcessID, "name": entry.szExeFile, "error": str(exc)})
                    finally:
                        api.CloseHandle(handle)
            found = api.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(snapshot)
    return {"status": "uncertain" if uncertain else "observed", "matches": matches, "uncertain": uncertain}


def owned_processes(process):
    """Snapshot job membership with process creation identity; never adopt outsiders."""
    api = observation_api()
    count = 64
    for _ in range(8):
        class PIDLIST(ctypes.Structure):
            _fields_ = [("assigned", w.DWORD), ("listed", w.DWORD), ("pids", ctypes.c_size_t * count)]
        data = PIDLIST()
        if process.api.QueryInformationJobObject(process.job, 3, ctypes.byref(data), ctypes.sizeof(data), None):
            rows = []
            for pid in data.pids[:data.listed]:
                try:
                    handle = open_snapshot_process(api, pid)
                except OSError as exc:
                    rows.append({"pid": pid, "status": "unobservable",
                                 "error": getattr(exc, 'winerror', None) or str(exc)})
                    continue
                if handle is None:
                    continue
                try:
                    row = live_process_identity(api, handle, pid, process.job)
                    if row is not None:
                        rows.append(row)
                except OSError as exc:
                    rows.append({"pid": pid, "status": "unobservable", "error": str(exc)})
                finally:
                    api.CloseHandle(handle)
            return rows
        if ctypes.get_last_error() != 234:
            raise ctypes.WinError(ctypes.get_last_error())
        count = max(count * 2, data.assigned + 16)
    raise OSError("job membership changed too quickly to observe")


def verify_observed_membership(process, observations):
    """Recheck handles directly; two independently sampled PID lists can race."""
    api = observation_api()
    rows = []
    for previous in observations:
        handle = open_snapshot_process(api, previous["pid"])
        if handle is None:
            continue
        try:
            row = live_process_identity(api, handle, previous["pid"], process.job)
            if row is None:
                continue
            if row["creation_ticks"] != previous["creation_ticks"]:
                raise OSError("process identity changed during ownership observation")
            rows.append(row)
        finally:
            api.CloseHandle(handle)
    return rows
