"""Foreground requests for verified run identities; no keys, clicks or settings."""
import ctypes
from ctypes import wintypes as w
import os
import re

from .windows_observation import observation_api, live_process_identity


def allow_foreground(pid):
    if os.name != 'nt' or not pid:
        return False
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.AllowSetForegroundWindow.argtypes = [w.DWORD]
    api.AllowSetForegroundWindow.restype = w.BOOL
    return bool(api.AllowSetForegroundWindow(pid))


def select_identities(rows, paths):
    """Priority is game, then launcher; never infer identity from process name."""
    for path in paths:
        if path:
            matches = [row for row in rows if row.get('creation_ticks') and row.get('image')
                       and os.path.normcase(row['image']) == os.path.normcase(path)]
            if matches:
                yield from matches


def request_foreground(rows, paths, window_filter=None):
    if os.name != 'nt':
        return {'status': 'unsupported'}
    api = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    signatures = {
        'EnumWindows': ([callback_type, w.LPARAM], w.BOOL),
        'GetWindowThreadProcessId': ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        'IsWindowVisible': ([w.HWND], w.BOOL),
        'IsIconic': ([w.HWND], w.BOOL),
        'ShowWindow': ([w.HWND, ctypes.c_int], w.BOOL),
        'SetForegroundWindow': ([w.HWND], w.BOOL),
        'GetForegroundWindow': ([], w.HWND),
        'GetWindowTextW': ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetClassNameW': ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes, getattr(api, name).restype = args, result
    windows = []

    @callback_type
    def collect(hwnd, _):
        if api.IsWindowVisible(hwnd):
            if window_filter:
                title, classname = ctypes.create_unicode_buffer(4096), ctypes.create_unicode_buffer(256)
                api.GetWindowTextW(hwnd, title, len(title))
                api.GetClassNameW(hwnd, classname, len(classname))
                if (not re.search(window_filter['window_regex'], title.value)
                        or not re.search(window_filter['class_regex'], classname.value)):
                    return True
            pid = w.DWORD()
            api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append((hwnd, pid.value))
        return True

    if not api.EnumWindows(collect, 0):
        return {'status': 'window_query_failed'}
    processes = observation_api()
    for previous in select_identities(rows, paths):
        candidates = [hwnd for hwnd, pid in windows if pid == previous['pid']]
        if not candidates:
            continue
        handle = processes.OpenProcess(0x101000, False, previous['pid'])
        if not handle:
            return {'status': 'identity_unconfirmed', 'pid': previous['pid']}
        try:
            current = live_process_identity(processes, handle, previous['pid'])
            if (current is None or current['creation_ticks'] != previous['creation_ticks']
                    or os.path.normcase(current['image']) != os.path.normcase(previous['image'])):
                return {'status': 'identity_changed', 'pid': previous['pid']}
            hwnd = candidates[0]
            if api.GetForegroundWindow() != hwnd:
                if api.IsIconic(hwnd):
                    api.ShowWindow(hwnd, 9)  # restore, no synthetic user input
                api.SetForegroundWindow(hwnd)
            return {'status': 'foreground' if api.GetForegroundWindow() == hwnd else 'foreground_denied',
                    'pid': previous['pid'], 'creation_ticks': previous['creation_ticks'], 'hwnd': int(hwnd)}
        except OSError:
            return {'status': 'identity_unconfirmed', 'pid': previous['pid']}
        finally:
            processes.CloseHandle(handle)
    return {'status': 'waiting_for_window'}
