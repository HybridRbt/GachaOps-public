"""Read-only desktop diagnostics. An accessible desktop does not prove capture works."""
import ctypes
from ctypes import wintypes as w
import os
from .windows_observation import elevation_report


def runtime_desktop_report():
    """Inspect the input desktop without switching it or sending input.

    A secure/inaccessible desktop is an environment interruption. Failure to
    open it alone does not establish that the user explicitly locked Windows.
    """
    if os.name != 'nt':
        return {'status': 'blocked', 'reason': 'windows_required'}
    try:
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        user.OpenInputDesktop.restype = w.HANDLE
        user.CloseDesktop.argtypes = [w.HANDLE]
        user.CloseDesktop.restype = w.BOOL
        user.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  w.DWORD, ctypes.POINTER(w.DWORD)]
        user.GetUserObjectInformationW.restype = w.BOOL
        desktop = user.OpenInputDesktop(0, False, 1)
        if not desktop:
            return {'status': 'blocked', 'reason': 'input_desktop_inaccessible',
                    'error': ctypes.get_last_error()}
        try:
            name, size = ctypes.create_unicode_buffer(256), w.DWORD()
            if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(size)):
                return {'status': 'blocked', 'reason': 'input_desktop_name_unknown',
                        'error': ctypes.get_last_error()}
            return {'status': 'observed_available' if name.value.casefold() == 'default' else 'blocked',
                    'reason': 'default_desktop' if name.value.casefold() == 'default' else 'input_desktop_not_default',
                    'input_desktop': name.value}
        finally:
            user.CloseDesktop(desktop)
    except OSError as exc:
        return {'status': 'blocked', 'reason': 'input_desktop_observation_failed', 'error': str(exc)}


def desktop_report():
    report = {"platform": os.name, "checks": {}, "blockers": [],
              "capture_verified": False, "remote_disconnect_verified": False}
    report["checks"]["privilege"] = elevation_report()
    if os.name != "nt":
        report.update(status="unsupported", blockers=["windows_required"])
        return report
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    kernel.GetCurrentProcessId.restype = w.DWORD
    kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
    kernel.ProcessIdToSessionId.restype = w.BOOL
    user.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    user.OpenInputDesktop.restype = w.HANDLE
    user.CloseDesktop.argtypes = [w.HANDLE]
    user.CloseDesktop.restype = w.BOOL
    user.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                             ctypes.POINTER(w.DWORD)]
    user.GetUserObjectInformationW.restype = w.BOOL
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    user.GetSystemMetrics.restype = ctypes.c_int
    wts.WTSQuerySessionInformationW.argtypes = [w.HANDLE, w.DWORD, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
    wts.WTSQuerySessionInformationW.restype = w.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wts.WTSFreeMemory.restype = None
    session = w.DWORD()
    if kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)):
        report["checks"]["session_id"] = session.value
        buffer, size = ctypes.c_void_p(), w.DWORD()
        if wts.WTSQuerySessionInformationW(None, session.value, 8, ctypes.byref(buffer), ctypes.byref(size)):
            try:
                state = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int)).contents.value
                report["checks"]["connection_state"] = state
                if session.value == 0 or state != 0:  # WTSActive
                    report["blockers"].append("session_not_active_interactive")
            finally:
                wts.WTSFreeMemory(buffer)
        else:
            report["blockers"].append("session_state_unknown")
    else:
        report["blockers"].append("session_id_unknown")
    desktop = user.OpenInputDesktop(0, False, 1)  # READOBJECTS only, never SwitchDesktop
    if desktop:
        try:
            name, size = ctypes.create_unicode_buffer(256), w.DWORD()
            if user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(size)):
                report["checks"]["input_desktop"] = name.value
                if name.value.casefold() != "default":
                    report["blockers"].append("input_desktop_not_default")
            else:
                report["blockers"].append("input_desktop_name_unknown")
        finally:
            user.CloseDesktop(desktop)
    else:
        report["checks"]["desktop_error"] = ctypes.get_last_error()
        report["blockers"].append("input_desktop_inaccessible")
    width, height = user.GetSystemMetrics(0), user.GetSystemMetrics(1)
    report["checks"]["primary_screen"] = {"width": width, "height": height}
    if width <= 0 or height <= 0:
        report["blockers"].append("primary_screen_unavailable")
    report["status"] = "blocked" if report["blockers"] else "observed_available"
    return report
