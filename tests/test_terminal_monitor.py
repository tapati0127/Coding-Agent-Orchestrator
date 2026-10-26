import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from orchestrator.terminal_monitor import app
from orchestrator.runtime.cli_logs import run_logged


class TerminalMonitorTests(unittest.TestCase):
    def test_runner_streams_before_exit_and_preserves_output_redacting_split_secret(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CLI_LOG_DIR': folder, 'TEST_API_KEY': 'private-token'}):
            # The child waits for an acknowledgement produced only after its log is visible.
            script = "import sys,time,pathlib; print('started',flush=True); p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+5; "
            script += "\nwhile not p.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            script += "assert p.exists(); sys.stdout.write('private-'); sys.stdout.flush(); time.sleep(.05); print('token'); print('err',file=sys.stderr)"
            marker = Path(folder) / 'ack'
            from threading import Thread
            def acknowledge():
                import time
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if any('started' in p.read_text() for p in Path(folder).glob('*.log')):
                        marker.touch()
                        return
                    time.sleep(.01)
            # Padding allows the redactor's short lookbehind to flush 'started'.
            script = script.replace("print('started',flush=True)", "print('started' + '.'*512,flush=True)")
            watcher = Thread(target=acknowledge)
            watcher.start()
            result = run_logged([sys.executable, '-c', script, str(marker)], env=os.environ.copy(), timeout=10,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
            watcher.join()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('private-token', result.stdout)
            self.assertIn('err', result.stderr)
            log = next(Path(folder).glob('*.log')).read_text()
            self.assertNotIn('private-token', log)
            self.assertIn('[REDACTED]', log)

    def test_monitor_reads_offsets_rotation_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CLI_LOG_DIR': folder}):
            path = Path(folder) / 'session.log'
            path.write_text('hello')
            (Path(folder) / 'link.log').symlink_to(path)
            with TestClient(app) as client:
                self.assertEqual(client.get('/terminals').status_code, 200)
                self.assertEqual(client.get('/terminals/api/files').json()['files'], ['session.log'])
                data = client.get('/terminals/api/read', params={'name':'session.log'}).json()
                self.assertEqual(base64.b64decode(data['data']), b'hello')
                self.assertEqual(client.get('/terminals/api/read', params={'name':'session.log','offset':-1}).status_code, 400)
                self.assertEqual(client.get('/terminals/api/read', params={'name':'../outside.log'}).status_code, 404)
                self.assertEqual(client.get('/terminals/api/read', params={'name':'link.log'}).status_code, 404)
                path.rename(Path(folder) / 'old.txt')
                path.write_text('new')
                data = client.get('/terminals/api/read', params={'name':'session.log','offset':5,'file_id':data['file_id']}).json()
                self.assertTrue(data['reset'])
                self.assertEqual(base64.b64decode(data['data']), b'new')
