import os
import sys
import subprocess
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from gachaops.windows_observation import live_process_identity, observation_api, open_snapshot_process, snapshot_pids
from gachaops import windows_observation
from gachaops.windows_process import WindowsProcess


class ProcessQueryRaceTests(unittest.TestCase):
    def test_matching_snapshot_failures_are_typed_observation_errors(self):
        for snapshot in (ctypes.c_void_p(-1).value, 77):
            with self.subTest(snapshot=snapshot):
                api = Mock(); api.CreateToolhelp32Snapshot.return_value = snapshot
                api.Process32FirstW.return_value = False
                with patch.object(windows_observation, 'os', SimpleNamespace(name='nt', path=os.path)), \
                     patch.object(windows_observation, 'observation_api', return_value=api), \
                     patch.object(windows_observation.ctypes, 'get_last_error', return_value=5, create=True), \
                     patch.object(windows_observation.ctypes, 'WinError', return_value=OSError('Access denied'), create=True):
                    with self.assertRaisesRegex(OSError, 'process_observation_unconfirmed'):
                        windows_observation.find_matching_processes(['game.exe'])
                if snapshot == 77:
                    api.CloseHandle.assert_called_once_with(77)
                else:
                    api.CloseHandle.assert_not_called()

    def test_invalid_pid_is_gone_only_after_new_snapshot_confirms_absence(self):
        def win_error(code):
            error = OSError('Windows error ' + str(code)); error.winerror = code
            return error
        for error, pids, gone in [(87, {456}, True), (87, {123}, False), (5, set(), False)]:
            with self.subTest(error=error, pids=pids), \
                 patch('gachaops.windows_observation.ctypes.get_last_error', return_value=error, create=True), \
                 patch('gachaops.windows_observation.ctypes.WinError', side_effect=win_error, create=True), \
                 patch('gachaops.windows_observation.snapshot_pids', return_value=pids) as snapshot:
                api = Mock(); api.OpenProcess.return_value = None
                if gone:
                    self.assertIsNone(open_snapshot_process(api, 123))
                else:
                    with self.assertRaises(OSError): open_snapshot_process(api, 123)
                self.assertEqual(snapshot.call_count, 1 if error == 87 else 0)
                api.CloseHandle.assert_not_called()
        api = Mock(); api.OpenProcess.return_value = None
        with patch('gachaops.windows_observation.ctypes.get_last_error', return_value=87, create=True), \
             patch('gachaops.windows_observation.snapshot_pids', side_effect=OSError('snapshot failed')):
            with self.assertRaisesRegex(OSError, 'snapshot failed'): open_snapshot_process(api, 123)

    def test_fresh_snapshot_requires_complete_enumeration_and_closes_handle(self):
        def populate(snapshot, pointer):
            pointer._obj.th32ProcessID = 123
            return True
        for final_error in (18, 5):
            with self.subTest(final_error=final_error):
                api = Mock(); api.CreateToolhelp32Snapshot.return_value = 77
                api.Process32FirstW.side_effect = populate; api.Process32NextW.return_value = False
                with patch('gachaops.windows_observation.ctypes.get_last_error', return_value=final_error, create=True), \
                     patch('gachaops.windows_observation.ctypes.WinError', return_value=OSError('snapshot incomplete'), create=True):
                    if final_error == 18:
                        self.assertEqual(snapshot_pids(api), {123})
                    else:
                        with self.assertRaises(OSError): snapshot_pids(api)
                api.CloseHandle.assert_called_once_with(77)

    def test_query_denied_is_ignored_only_when_same_handle_signals_exit(self):
        for final_wait in (0, 0x102, 0xffffffff):
            with self.subTest(final_wait=final_wait):
                api = Mock()
                api.WaitForSingleObject.side_effect = [0x102, final_wait]
                with patch('gachaops.windows_observation.process_identity',
                           side_effect=PermissionError('identity query denied')):
                    if final_wait == 0:
                        self.assertIsNone(live_process_identity(api, 77, 123))
                    else:
                        with self.assertRaises(PermissionError):
                            live_process_identity(api, 77, 123)
                self.assertEqual(api.WaitForSingleObject.call_args_list[0].args, (77, 0))
                self.assertEqual(api.WaitForSingleObject.call_args_list[1].args, (77, 2000))

    def test_live_candidate_transient_query_denial_revalidates_same_handle(self):
        api = Mock()
        api.WaitForSingleObject.side_effect = [0x102, 0x102]
        identity = {'pid': 123, 'creation_ticks': 456, 'image': 'game.exe', 'in_owned_job': True}
        with patch('gachaops.windows_observation.process_identity',
                   side_effect=[PermissionError('transient denial'), identity]) as query:
            self.assertEqual(live_process_identity(api, 77, 123, 99), identity)
        self.assertEqual([call.args for call in query.call_args_list], [(api, 77, 123, 99)] * 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows process handles required')
    def test_real_candidate_exits_before_opening_snapshot_pid(self):
        with WindowsProcess([sys.executable, '-c', 'pass']) as candidate:
            pid = candidate.pid
            self.assertEqual(candidate.wait(10), 0)
        # Toolhelp could have returned this PID before exit. Windows may still
        # retain the dead process object; either absence or a signalled handle
        # is valid exit evidence, and any returned handle must be closed.
        api = observation_api()
        handle = open_snapshot_process(api, pid)
        if handle is not None:
            try:
                self.assertIsNone(live_process_identity(api, handle, pid))
            finally:
                api.CloseHandle(handle)

    @unittest.skipUnless(os.name == 'nt', 'Windows process handles required')
    def test_delayed_candidate_exit_does_not_wait_for_live_assistant_root(self):
        # Two real processes: the observed game fixture exits shortly after
        # query denial while the assistant fixture stays alive. No game runs.
        with WindowsProcess([sys.executable, '-c', 'import time; time.sleep(30)']) as root, \
             WindowsProcess([sys.executable, '-c', 'import time; time.sleep(0.4)']) as candidate:
            api = observation_api()
            with patch('gachaops.windows_observation.process_identity',
                       side_effect=PermissionError('simulated teardown query denial')):
                self.assertIsNone(live_process_identity(api, candidate.process, candidate.pid, candidate.job))
            with self.assertRaises(subprocess.TimeoutExpired):
                root.wait(0)

    @unittest.skipUnless(os.name == 'nt', 'Windows process handles required')
    def test_real_owned_process_exits_between_wait_and_identity_query(self):
        # No game or assistant: natural exit of this fixture is synchronized
        # precisely between the two APIs that raced in the GUI report.
        with WindowsProcess([sys.executable, '-c', 'import time; time.sleep(1)']) as process:
            api = observation_api()

            def exit_during_query(*args):
                self.assertEqual(process.wait(10), 0)
                raise PermissionError('simulated query after actual process exit')

            with patch('gachaops.windows_observation.process_identity', side_effect=exit_during_query):
                self.assertIsNone(live_process_identity(api, process.process, process.pid, process.job))


if __name__ == '__main__':
    unittest.main()
