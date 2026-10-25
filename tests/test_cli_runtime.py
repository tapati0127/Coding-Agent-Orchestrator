from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.runtime.cli import run_llm_cli


class CliRuntimeTests(unittest.TestCase):
    def test_both_clis_always_autoapprove_and_cleanup_preserving_project(self):
        for cli in ('qwen', 'opencode'):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as workspace:
                observed = []
                def run(command, **kwargs):
                    workdir = Path(kwargs['cwd'])
                    observed.append(workdir)
                    self.assertNotEqual(workdir, Path(workspace))
                    self.assertEqual((workdir / 'project').resolve(), Path(workspace))
                    (workdir / 'scratch.txt').write_text('temporary')
                    (workdir / 'project' / 'result.txt').write_text('keep changes')
                    self.assertEqual(kwargs['env']['TMPDIR'], str(workdir / 'tmp'))
                    self.assertIn('Original task', command[-1])
                    if cli == 'qwen':
                        self.assertIn('--yolo', command)
                    else:
                        config = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                        self.assertEqual(config['permission'], 'allow')
                        self.assertEqual(json.loads(kwargs['env']['OPENCODE_PERMISSION']), {'*': 'allow'})
                    self.assertNotIn('secret-key', command)
                    return subprocess.CompletedProcess(command, 0, 'Done', '')
                with patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/' + cli), patch('orchestrator.runtime.cli.subprocess.run', side_effect=run):
                    result = run_llm_cli('Original task', cli, base_url='http://internal/v1', model='coder', api_key='secret-key', cwd=workspace)
                self.assertEqual(result, 'Done')
                self.assertFalse(observed[0].exists())
                self.assertEqual((Path(workspace) / 'result.txt').read_text(), 'keep changes')

    def test_cleanup_on_process_failure_and_timeout(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out), tempfile.TemporaryDirectory() as workspace:
                observed = []
                def run(command, **kwargs):
                    observed.append(Path(kwargs['cwd']))
                    if timed_out:
                        raise subprocess.TimeoutExpired(command, 1)
                    return subprocess.CompletedProcess(command, 1, '', 'Failed secret-key')
                with patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/qwen'), patch('orchestrator.runtime.cli.subprocess.run', side_effect=run):
                    with self.assertRaises(subprocess.TimeoutExpired if timed_out else RuntimeError) as error:
                        run_llm_cli('Task', base_url='http://internal/v1', model='coder', api_key='secret-key', cwd=workspace)
                self.assertFalse(observed[0].exists())
                if not timed_out:
                    self.assertNotIn('secret-key', str(error.exception))

    def test_concurrent_calls_have_different_workdirs(self):
        from threading import Barrier
        barrier = Barrier(2)
        observed = []
        def run(command, **kwargs):
            observed.append(Path(kwargs['cwd']))
            barrier.wait(timeout=2)
            self.assertTrue(Path(kwargs['cwd']).is_dir())
            return subprocess.CompletedProcess(command, 0, 'Done', '')
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/qwen'), patch('orchestrator.runtime.cli.subprocess.run', side_effect=run):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: run_llm_cli('Task', base_url='http://internal/v1', model='coder', cwd=workspace), range(2)))
        self.assertEqual(results, ['Done', 'Done'])
        self.assertEqual(len(set(observed)), 2)
        self.assertTrue(all(not path.exists() for path in observed))
