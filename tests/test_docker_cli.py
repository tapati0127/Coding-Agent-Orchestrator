import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.runtime.cli import run_llm_cli


class DockerCliTests(unittest.TestCase):
    def test_cli_in_container_mounts_project_and_ca_without_keys_in_argv(self):
        with tempfile.TemporaryDirectory() as root:
            project=Path(root)/'project'
            project.mkdir()
            cert=Path(root)/'ca.pem'
            cert.write_text('test certificate')
            observed=[]
            def run(command, **kwargs):
                observed.append(command)
                if command[1] == 'run':
                    cidfile=Path(command[command.index('--cidfile')+1])
                    cidfile.write_text('test-container')
                    self.assertIn('qwen', command)
                    self.assertIn('--yolo', command)
                    self.assertIn(f'type=bind,src={project},dst={project}', command)
                    self.assertIn(f'type=bind,src={cert},dst={cert},readonly', command)
                    self.assertNotIn('top-secret', command)
                    self.assertEqual(kwargs['env']['OPENAI_API_KEY'], 'top-secret')
                    return subprocess.CompletedProcess(command,0,'Done','')
                return subprocess.CompletedProcess(command,0,'','')
            with patch.dict(os.environ, {'CODING_CLI_RUNTIME':'docker', 'SSL_CERT_FILE':str(cert), 'NODE_EXTRA_CA_CERTS':str(cert)}), patch('orchestrator.runtime.cli.shutil.which', side_effect=lambda key: '/bin/docker' if key == 'docker' else None), patch('orchestrator.runtime.docker_cli.subprocess.run', side_effect=run):
                self.assertEqual(run_llm_cli('Task', cwd=project, base_url='http://internal/v1', model='coder', api_key='top-secret'), 'Done')
            self.assertEqual(observed[-1], ['/bin/docker','rm','--force','test-container'])
            workdir=Path(observed[0][observed[0].index('--workdir')+1])
            self.assertFalse(workdir.exists())

    def test_timeout_removes_container_and_scratch(self):
        observed=[]
        def run(command, **kwargs):
            observed.append(command)
            if command[1] == 'run':
                Path(command[command.index('--cidfile')+1]).write_text('timeout-container')
                raise subprocess.TimeoutExpired(command, 1)
            return subprocess.CompletedProcess(command,0,'','')
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CODING_CLI_RUNTIME':'docker','SSL_CERT_FILE':'','NODE_EXTRA_CA_CERTS':''}), patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/docker'), patch('orchestrator.runtime.docker_cli.subprocess.run', side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_llm_cli('Task','opencode',cwd=root,base_url='http://internal/v1',model='coder')
        self.assertEqual(observed[-1], ['/bin/docker','rm','--force','timeout-container'])
        self.assertFalse(Path(observed[0][observed[0].index('--workdir')+1]).exists())

    def test_linked_worktree_mounts_common_git_metadata(self):
        from orchestrator.runtime.git_tasks import git
        from orchestrator.runtime.docker_cli import run_in_docker
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo, project, scratch = root / 'repo', root / 'task', root / 'scratch'
            repo.mkdir()
            scratch.mkdir()
            git(repo, 'init', '-b', 'main')
            (repo / 'file.txt').write_text('base')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Initial')
            git(repo, 'worktree', 'add', '-b', 'task', str(project))
            real_run = subprocess.run
            observed = []
            def run(command, **kwargs):
                if command[0] == 'git':
                    return real_run(command, **kwargs)
                observed.append(command)
                return subprocess.CompletedProcess(command, 0, 'Done', '')
            with patch('orchestrator.runtime.docker_cli.subprocess.run', side_effect=run):
                run_in_docker(['qwen'], docker='/bin/docker', workdir=scratch, project=project,
                              env={}, timeout=5)
            common = repo / '.git'
            self.assertIn(f'type=bind,src={common},dst={common}', observed[0])
