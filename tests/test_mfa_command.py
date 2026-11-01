import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace

from gachaops.mfa_command import send_start
from gachaops.windows_process import WindowsProcess


@unittest.skipUnless(os.name == 'nt', 'Windows named-pipe fixture')
class NativeCommandTests(unittest.TestCase):
    def test_owned_server_acknowledgement_rejection_and_bounded_timeout(self):
        for reply in ('OK', 'FAILED', 'SILENT', 'WRONG_PID'):
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as directory:
                key = uuid.uuid4().hex
                destination = Path(directory) / 'request.json'
                argv = [sys.executable, str(Path(__file__).parent / 'fixtures/mfa_pipe.py'), key,
                        str(destination), 'OK' if reply == 'WRONG_PID' else reply]
                with WindowsProcess(argv) as process:
                    deadline = time.monotonic() + 10
                    while not Path(str(destination) + '.ready').exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    start = time.monotonic()
                    if reply == 'OK':
                        result = send_start(process, key, 'default')
                        self.assertEqual(result['pipe_server_pid'], process.pid)
                        self.assertEqual(result['requests'], 1)
                        self.assertEqual(process.wait(10), 0)
                    else:
                        target = SimpleNamespace(pid=os.getpid(), api=process.api, process=process.process) if reply == 'WRONG_PID' else process
                        with self.assertRaises(ValueError):
                            send_start(target, key, 'default', timeout=0.2)
                        self.assertLess(time.monotonic() - start, 2)
                    if reply == 'WRONG_PID':
                        self.assertFalse(destination.exists())
                    else:
                        request = json.loads(destination.read_text())
                        self.assertEqual(request, {'AutoStart': True, 'QuitAfterRun': True,
                                                   'ForceStart': False, 'InstanceSelector': 'default'})
