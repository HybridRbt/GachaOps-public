import unittest
import os
from unittest.mock import Mock, patch
from gachaops.foreground import select_identities


class ForegroundTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows callback types required')
    def test_startup_window_filter_rejects_splash_and_wrong_class(self):
        from gachaops.foreground import request_foreground
        for title, classname, accepted in [('Splash', 'UnityWndClass', False),
                                           ('EXILIUM', 'OtherClass', False),
                                           ('EXILIUM', 'UnityWndClass', True)]:
            with self.subTest(title=title, classname=classname):
                user = Mock()
                user.IsWindowVisible.return_value = True
                user.GetWindowTextW.side_effect = lambda hwnd, buffer, size: setattr(buffer, 'value', title) or len(title)
                user.GetClassNameW.side_effect = lambda hwnd, buffer, size: setattr(buffer, 'value', classname) or len(classname)
                user.GetWindowThreadProcessId.side_effect = lambda hwnd, out: setattr(out._obj, 'value', 22)
                user.EnumWindows.side_effect = lambda callback, arg: callback(77, arg)
                user.GetForegroundWindow.return_value = 77
                identity = {'pid': 22, 'image': 'game.exe', 'creation_ticks': 10}
                with patch('gachaops.foreground.ctypes.WinDLL', return_value=user), \
                     patch('gachaops.foreground.observation_api'), \
                     patch('gachaops.foreground.live_process_identity', return_value=identity):
                    result = request_foreground([identity], ['game.exe'],
                                                window_filter={'window_regex': '^EXILIUM$', 'class_regex': '^UnityWndClass$'})
                self.assertEqual(result['status'], 'foreground' if accepted else 'waiting_for_window')
                user.SetForegroundWindow.assert_not_called()

    def test_game_precedes_launcher_and_unknown_names_are_not_selected(self):
        rows = [{'pid': 2, 'image': 'launcher.exe', 'creation_ticks': 12},
                {'pid': 3, 'image': 'other/game.exe', 'creation_ticks': 13},
                {'pid': 4, 'image': 'game.exe'},
                {'pid': 1, 'image': 'game.exe', 'creation_ticks': 11}]
        self.assertEqual([r['pid'] for r in select_identities(rows, ['game.exe', 'launcher.exe'])], [1, 2])

    @unittest.skipUnless(os.name == 'nt', 'Windows callback types required')
    def test_reused_pid_never_receives_foreground_and_denial_is_reported(self):
        from gachaops.foreground import request_foreground
        for reused in (True, False):
            with self.subTest(reused=reused):
                user = Mock()
                user.IsWindowVisible.return_value = True
                user.GetWindowThreadProcessId.side_effect = lambda hwnd, out: setattr(out._obj, 'value', 22)
                user.EnumWindows.side_effect = lambda callback, arg: callback(77, arg)
                user.GetForegroundWindow.return_value = 88
                user.IsIconic.return_value = False
                processes = Mock()
                current = {'pid': 22, 'image': 'game.exe', 'creation_ticks': 11 if reused else 10}
                with patch('gachaops.foreground.ctypes.WinDLL', return_value=user), \
                     patch('gachaops.foreground.observation_api', return_value=processes), \
                     patch('gachaops.foreground.live_process_identity', return_value=current):
                    result = request_foreground([dict(current, creation_ticks=10)], ['game.exe'])
                self.assertEqual(result['status'], 'identity_changed' if reused else 'foreground_denied')
                if reused:
                    user.SetForegroundWindow.assert_not_called()
                else:
                    user.SetForegroundWindow.assert_called_once_with(77)


if __name__ == '__main__':
    unittest.main()
