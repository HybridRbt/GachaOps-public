"""Single bounded request to MFAAvalonia's native pipe, verified against our root."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
import time


class Overlapped(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', w.DWORD), ('OffsetHigh', w.DWORD), ('hEvent', w.HANDLE)]


def instance_key(exe, core):
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest().upper() for p in (exe, core)]
    return hashlib.sha256(('\n'.join([str(exe.resolve()).rstrip('\\/').upper()] + hashes)).encode()).hexdigest().upper()


def pipe_api():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateFileW': ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        'GetNamedPipeServerProcessId': ([w.HANDLE, ctypes.POINTER(w.ULONG)], w.BOOL),
        'CreateEventW': ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        'ResetEvent': ([w.HANDLE], w.BOOL),
        'ReadFile': ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)], w.BOOL),
        'WriteFile': ([w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped)], w.BOOL),
        'GetOverlappedResult': ([w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL], w.BOOL),
        'CancelIoEx': ([w.HANDLE, ctypes.POINTER(Overlapped)], w.BOOL),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
        'CloseHandle': ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes, getattr(api, name).restype = args, result
    return api


def send_start(process, key, instance, *, timeout=3, stop_requested=None):
    if os.name != 'nt':
        raise OSError('native MFA pipe requires Windows')
    api = pipe_api()
    handle = api.CreateFileW('\\\\.\\pipe\\MFAAvalonia_' + key, 0xC0000000, 0, None, 3, 0x40000000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError('native command pipe unavailable; command not sent')
    event = None
    deadline = time.monotonic() + timeout
    try:
        server = w.ULONG()
        if (not api.GetNamedPipeServerProcessId(handle, ctypes.byref(server))
                or server.value != process.pid or process.api.WaitForSingleObject(process.process, 0) != 0x102):
            raise ValueError('native command pipe does not belong to live owned root')
        event = api.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())

        def transfer(function, buffer, size):
            overlap, count = Overlapped(), w.DWORD()
            overlap.hEvent = event
            if not api.ResetEvent(event):
                raise ctypes.WinError(ctypes.get_last_error())
            if not function(handle, buffer, size, ctypes.byref(count), ctypes.byref(overlap)):
                if ctypes.get_last_error() != 997:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    while True:
                        result = api.WaitForSingleObject(event, 50)
                        if result == 0:
                            break
                        if result != 0x102:
                            raise ctypes.WinError(ctypes.get_last_error())
                        if stop_requested and stop_requested():
                            raise ValueError('native command cancelled; no retry')
                        if time.monotonic() >= deadline:
                            raise ValueError('native command acknowledgement timeout; no retry')
                except BaseException:
                    api.CancelIoEx(handle, ctypes.byref(overlap))
                    # Keep buffers alive until this cancelled operation completes.
                    api.GetOverlappedResult(handle, ctypes.byref(overlap), ctypes.byref(count), True)
                    raise
            if not api.GetOverlappedResult(handle, ctypes.byref(overlap), ctypes.byref(count), True):
                raise ctypes.WinError(ctypes.get_last_error())
            return count.value

        payload = json.dumps({'AutoStart': True, 'QuitAfterRun': True, 'ForceStart': False,
                              'InstanceSelector': instance}, separators=(',', ':')).encode() + b'\n'
        if stop_requested and stop_requested():
            raise ValueError('native command cancelled before send')
        data = ctypes.create_string_buffer(payload)
        if transfer(api.WriteFile, data, len(payload)) != len(payload):
            raise ValueError('incomplete native command write; no retry')
        api.CloseHandle(event)
        event = api.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        response = bytearray()
        while b'\n' not in response and len(response) < 64:
            if time.monotonic() >= deadline:
                raise ValueError('native command acknowledgement timeout; no retry')
            buffer = ctypes.create_string_buffer(1)
            if transfer(api.ReadFile, buffer, 1) != 1:
                raise ValueError('native command disconnected; no retry')
            response.extend(buffer.raw)
        if bytes(response) != b'OK\n' and bytes(response) != b'OK\r\n':
            raise ValueError('native command rejected; no retry')
        return {'status': 'acknowledged', 'root_pid': process.pid, 'instance_id': instance,
                'pipe_server_pid': server.value, 'force_start': False, 'requests': 1}
    finally:
        if event:
            api.CloseHandle(event)
        api.CloseHandle(handle)
