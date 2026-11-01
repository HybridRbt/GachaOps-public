"""Ordinary Python native-pipe fixture; never imports any assistant or game."""
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import time

api = ctypes.WinDLL('kernel32', use_last_error=True)
api.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p]
api.CreateNamedPipeW.restype = w.HANDLE
api.ConnectNamedPipe.argtypes = [w.HANDLE, ctypes.c_void_p]
api.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
api.WriteFile.argtypes = api.ReadFile.argtypes
api.CloseHandle.argtypes = [w.HANDLE]
key, destination, reply = sys.argv[1:]
handle = api.CreateNamedPipeW('\\\\.\\pipe\\MFAAvalonia_' + key, 3, 0, 1, 4096, 4096, 0, None)
assert handle != ctypes.c_void_p(-1).value
Path(destination + '.ready').touch()
assert api.ConnectNamedPipe(handle, None) or ctypes.get_last_error() == 535
buffer, count = ctypes.create_string_buffer(4096), w.DWORD()
assert api.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None)
Path(destination).write_text(json.dumps(json.loads(buffer.raw[:count.value])), encoding='utf-8')
if reply == 'SILENT':
    time.sleep(5)
else:
    data = ctypes.create_string_buffer((reply + '\r\n').encode())
    assert api.WriteFile(handle, data, len(reply) + 2, ctypes.byref(count), None)
    time.sleep(0.3)
api.CloseHandle(handle)
