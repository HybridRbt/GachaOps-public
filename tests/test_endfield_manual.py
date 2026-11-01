import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gachaops import maaend
from gachaops.assistants import load_assistants


class EndfieldManualTests(unittest.TestCase):
    def test_manual_entry_keeps_native_preset_and_borrows_exact_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'config').mkdir(); (root / 'maafw').mkdir()
            (root / 'maafw/MaaFramework.dll').write_bytes(b'fixture')
            (root / 'MaaEnd.exe').write_bytes(b'--autostart --instance --quit-after-run')
            (root / 'interface.json').write_text('{"version":"fixture"}')
            game = root / 'Endfield.exe'; game.write_bytes(b'fixture')
            action = dict(enabled=False, program=str(game), useCmd=False)
            instance = dict(id='daily', name='daily', tasks='opaque', preActions=[action])
            config = dict(lastActiveInstanceId='daily', instances=[instance])
            path = root / 'config/mxu-MaaEnd.json'
            row = dict(id='endfield', adapter='maa-end', installation_root=str(root), game_start='manual')
            registration = root / 'assistants.json'
            registration.write_text(json.dumps(dict(schema_version=1, assistants=[row])))
            self.assertEqual(load_assistants(registration)['assistants'][0]['game_start'], 'manual')
            identity = dict(pid=123, creation_ticks=456, image=str(game))
            with patch.object(maaend, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(maaend, 'desktop_report', return_value={'status': 'observed_available'}), \
                 patch.object(maaend, 'find_matching_processes') as observe:
                def prepare(matches, status='observed'):
                    path.write_text(json.dumps(config))
                    before = path.read_bytes()
                    observe.return_value = dict(status=status, matches=matches)
                    result = maaend.prepare(row)
                    self.assertEqual(path.read_bytes(), before)
                    return result
                current = prepare([identity])
                self.assertEqual(current['blockers'], [])
                self.assertEqual(current['borrowed_game'], [identity])
                self.assertEqual(current['game_start'], 'manual')
                self.assertIn('manual_game_requires_exactly_one_running_instance', prepare([])['blockers'])
                self.assertIn('manual_game_requires_exactly_one_running_instance', prepare([identity, dict(identity, pid=124)])['blockers'])
                self.assertIn('existing_runner_not_exclusive', prepare([identity, dict(identity, image=str(root / 'MaaEnd.exe'))])['blockers'])
                self.assertIn('existing_runner_or_game_not_exclusive', prepare([], 'uncertain')['blockers'])
                action['enabled'] = True
                self.assertIn('maaend_disable_launch_preactions', prepare([identity])['blockers'])
                action['enabled'] = False
                instance['preActions'].append(dict(enabled=True, program='cmd.exe'))
                self.assertIn('maaend_disable_launch_preactions', prepare([identity])['blockers'])
                instance['preActions'].pop()
                row['game_start'] = 'assistant'; action['enabled'] = True
                self.assertIn('existing_runner_or_game_not_exclusive', prepare([identity])['blockers'])
                self.assertEqual(prepare([])['borrowed_game'], [])
