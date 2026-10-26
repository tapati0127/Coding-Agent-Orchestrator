import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from orchestrator.terminal_monitor import app
from orchestrator.runtime.cli_logs import run_logged, StreamRedactor
from orchestrator.runtime.cli import run_llm_cli


class TerminalMonitorTests(unittest.TestCase):
    def test_runner_streams_before_exit_and_preserves_output_redacting_split_secret(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'CLI_LOG_DIR': folder, 'TEST_API_KEY': 'private-token', 'LONG_SECRET': 'x' * 2048}):
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

    def test_native_qwen_progress_and_task_title_are_available_before_cli_exits(self):
        from threading import Thread
        import time
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            logs, project = root / 'logs', root / 'project'
            logs.mkdir()
            project.mkdir()
            marker = project / 'ack'
            cli = root / 'qwen'
            first = {'type': 'stream_event', 'event': {'type': 'content_block_delta',
                     'delta': {'type': 'text_delta', 'text': 'Đang đọc repository…\n'}}}
            assistant = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Đã sửa xong'}]}}
            result_event = {'type': 'result', 'is_error': False, 'result': 'Đã sửa xong'}
            cli.write_text(f'#!{sys.executable}\n' +
                'import json,os,pathlib,time\n' +
                'print(' + repr(json.dumps(first, ensure_ascii=False)) + ',flush=True)\n' +
                "p=pathlib.Path(os.environ['AGENT_PROJECT_DIR'])/'ack'\n" +
                'deadline=time.monotonic()+5\n' +
                'while not p.exists() and time.monotonic()<deadline: time.sleep(.01)\n' +
                'assert p.exists(), "Progress was unavailable before exit"\n' +
                'print(' + repr(json.dumps(assistant, ensure_ascii=False)) + ',flush=True)\n' +
                'print(' + repr(json.dumps(result_event, ensure_ascii=False)) + ',flush=True)\n')
            cli.chmod(0o700)
            observed = []
            with patch.dict(os.environ, {'CLI_LOG_DIR': str(logs), 'CODING_CLI_RUNTIME': 'local'}), TestClient(app) as client:
                # Include a pre-existing log to verify that the running task sorts first.
                (logs / 'orchestrator.log').write_text('system log')
                def acknowledge():
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        sessions = client.get('/terminals/api/files').json()['sessions']
                        if sessions and sessions[0]['status'] == 'running':
                            session = sessions[0]
                            data = client.get('/terminals/api/read', params={'name': session['name']}).json()
                            text = base64.b64decode(data['data']).decode()
                            if 'Đang đọc repository…' in text:
                                observed.append((session, text))
                                marker.touch()
                                return
                        time.sleep(.01)
                watcher = Thread(target=acknowledge)
                watcher.start()
                try:
                    with patch('orchestrator.runtime.cli.shutil.which', return_value=str(cli)):
                        result = run_llm_cli('Task', cwd=project, base_url='http://internal/v1', model='coder',
                            role='execute', task_title='Sửa đăng nhập', task_id='login', timeout=10)
                finally:
                    watcher.join()
                self.assertEqual(result, 'Đã sửa xong')
                self.assertEqual(len(observed), 1)
                session, text = observed[0]
                self.assertEqual(session['title'], 'Sửa đăng nhập')
                self.assertEqual(session['role'], 'Execute')
                self.assertEqual(session['task_id'], 'login')
                self.assertNotIn('content_block_delta', text)
                session = next(s for s in client.get('/terminals/api/files').json()['sessions'] if s['name'] == session['name'])
                self.assertEqual(session['status'], 'completed')
                self.assertIn('ended_at', session)

    def test_secret_redaction_at_every_chunk_boundary_does_not_delay_ordinary_text(self):
        secrets = ['private-token', 'private-token-long', '密钥"token']
        text = 'hello private-token-long; private-token; 密钥"token; done\n'
        expected = 'hello [REDACTED]; [REDACTED]; [REDACTED]; done\n'
        for boundary in range(len(text) + 1):
            with self.subTest(boundary=boundary):
                redactor = StreamRedactor(secrets)
                output = redactor.feed(text[:boundary]) + redactor.feed(text[boundary:], final=True)
                self.assertEqual(output, expected)
        self.assertEqual(StreamRedactor(secrets).feed('started\n'), 'started\n')

    def test_sessions_record_launch_failure_nonzero_and_timeout(self):
        for mode in ('launch', 'failed', 'timed_out'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                env = {**os.environ, 'CLI_LOG_DIR': folder, 'TEST_API_KEY': 'private-token'}
                command = ['/missing/cli'] if mode == 'launch' else [sys.executable, '-c',
                    'import sys;sys.exit(2)' if mode == 'failed' else 'import time;time.sleep(10)']
                try:
                    run_logged(command, env=env, timeout=.1,
                               log_info={'title': 'Task private-token', 'role': 'Execute'})
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                session = json.loads(next(Path(folder).glob('*.log.json')).read_text())
                self.assertEqual(session['status'], 'timed_out' if mode == 'timed_out' else 'failed')
                self.assertNotIn('private-token', session['title'])
                self.assertIn('ended_at', session)

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
                # Sidecar metadata cannot escape the log directory through a symlink.
                external = Path(folder) / 'outside.json'
                external.write_text('{"title":"Must not be displayed"}')
                (Path(folder) / 'session.log.json').symlink_to(external)
                sessions = client.get('/terminals/api/files').json()['sessions']
                self.assertEqual(sessions[0]['title'], 'Phiên CLI trước đây')
                path.rename(Path(folder) / 'old.txt')
                path.write_text('new')
                data = client.get('/terminals/api/read', params={'name':'session.log','offset':5,'file_id':data['file_id']}).json()
                self.assertTrue(data['reset'])
                self.assertEqual(base64.b64decode(data['data']), b'new')
