import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/synthetic_adapter.py'


class SyntheticContractTests(unittest.TestCase):
    def fixture_module(self):
        spec = importlib.util.spec_from_file_location('synthetic_fixture_adapter', FIXTURE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_completion_requires_run_owned_writer_freshness_and_terminal_result(self):
        fixture = self.fixture_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'fixture with spaces'; root.mkdir()
            for mode, expected in [('success', 'success'), ('partial_failure', 'partial_failure'), ('ambiguous', 'completion_unconfirmed')]:
                output = root / (mode + '.json')
                cursor = {'run_id': 'synthetic-run', 'started_at_ns': time.time_ns(), 'output': str(output)}
                process = subprocess.Popen([sys.executable, str(FIXTURE), '--run-id', cursor['run_id'],
                    '--mode', mode, '--output', str(output)], shell=False)
                self.assertEqual(process.wait(timeout=10), 0)
                receipt = json.loads(output.read_text())
                owned = [{'pid': process.pid, 'creation_ticks': receipt['creation_ticks']}]
                self.assertEqual(fixture.observe(cursor, 0, process.pid, owned), expected)
                self.assertEqual(fixture.observe(dict(cursor, run_id='old-run'), 0, process.pid, owned), 'completion_unconfirmed')
                self.assertEqual(fixture.observe(cursor, 0, process.pid, []), 'completion_unconfirmed')
                self.assertEqual(fixture.observe(cursor, 7, process.pid, owned), 'failed')
                self.assertEqual(fixture.observe(dict(cursor, started_at_ns=time.time_ns()+10**9), 0, process.pid, owned), 'completion_unconfirmed')

    def test_timeout_and_cancel_only_stop_the_synthetic_process(self):
        self.fixture_module()
        with tempfile.TemporaryDirectory() as temp:
            for operation in ('timeout', 'cancel'):
                process = subprocess.Popen([sys.executable, str(FIXTURE), '--run-id', 'fixture',
                    '--mode', 'timeout', '--output', str(Path(temp) / (operation + '.json'))], shell=False)
                try:
                    if operation == 'timeout':
                        with self.assertRaises(subprocess.TimeoutExpired): process.wait(timeout=0.1)
                    process.terminate(); process.wait(timeout=10)
                    self.assertIsNotNone(process.returncode)
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=10)
