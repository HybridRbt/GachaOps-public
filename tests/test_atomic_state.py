import ctypes
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from gachaops.recovery import save


def denial(code):
    error = PermissionError('fixture destination locked')
    error.winerror = code
    return error


class AtomicStateTests(unittest.TestCase):
    def test_transient_windows_replace_lock_keeps_old_file_until_atomic_success(self):
        for code in (5, 32, 33):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'execution.json'
                path.write_text('{"phase": "old"}')
                calls = []
                def replace(source, target):
                    calls.append(Path(source))
                    self.assertEqual(json.loads(path.read_text()), {'phase': 'old'})
                    if len(calls) < 3:
                        raise denial(code)
                    os.replace(source, target)
                with patch('gachaops.recovery.os', wraps=os) as api, patch('gachaops.recovery.time.sleep') as sleep:
                    api.name = 'nt'
                    api.replace.side_effect = replace
                    save(path, {'phase': 'new'})
                self.assertEqual(len(set(calls)), 1)
                self.assertEqual(sleep.call_count, 2)
                self.assertEqual(json.loads(path.read_text()), {'phase': 'new'})
                self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_permanent_denial_is_bounded_and_unrelated_errors_are_not_retried(self):
        for code, expected in ((5, 6), (87, 1)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'execution.json'
                path.write_text('{"phase": "old"}')
                with patch('gachaops.recovery.os', wraps=os) as api, patch('gachaops.recovery.time.sleep') as sleep:
                    api.name = 'nt'
                    api.replace.side_effect = denial(code)
                    with self.assertRaises(PermissionError):
                        save(path, {'phase': 'new'})
                    self.assertEqual(api.replace.call_count, expected)
                    self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1.25)
                self.assertEqual(json.loads(path.read_text()), {'phase': 'old'})
                self.assertEqual(list(path.parent.glob('*.tmp')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_actual_windows_reader_without_delete_sharing(self):
        from ctypes import wintypes as w
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        api.CreateFileW.restype = w.HANDLE
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'execution.json'
            save(path, {'phase': 'old'})
            handle = api.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            released = threading.Event()
            def release():
                api.CloseHandle(handle)
                released.set()
            timer = threading.Timer(0.2, release)
            timer.start()
            try:
                save(path, {'phase': 'new'})
                self.assertTrue(released.is_set())
                self.assertEqual(json.loads(path.read_text()), {'phase': 'new'})
            finally:
                timer.join()
