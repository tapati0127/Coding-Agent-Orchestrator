import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.runtime.cli import run_llm_cli
from orchestrator.runtime.cli_skills import configured_skills


def skill(path, name='sample-skill'):
    path.mkdir(parents=True)
    (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test reusable workflow\n---\nSkill instructions')
    (path / 'helper.py').write_text('print("helper")')


class CliSkillsTests(unittest.TestCase):
    def test_separate_configs_stage_assets_and_cleanup_for_both_clis(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            configs = root / 'config'
            configs.mkdir()
            workspace = root / 'repo'
            workspace.mkdir()
            env = {}
            for cli in ('qwen', 'opencode'):
                skill(root / cli / f'{cli}-skill', f'{cli}-skill')
                config = configs / f'{cli}.yaml'
                config.write_text(f'skills:\n  paths: [../{cli}]\n')
                env[f'{cli.upper()}_SKILLS_CONFIG'] = str(config)
            observed = []
            for cli in ('qwen', 'opencode'):
                def run(command, **kwargs):
                    cwd = Path(kwargs['cwd'])
                    observed.append(cwd)
                    target = cwd / f'.{cli}' / 'skills' / f'{cli}-skill'
                    self.assertTrue((target / 'SKILL.md').is_file())
                    self.assertTrue((target / 'helper.py').is_file())
                    self.assertFalse((cwd / f'.{cli}' / 'skills' / ('opencode-skill' if cli == 'qwen' else 'qwen-skill')).exists())
                    if cli == 'qwen':
                        settings = json.loads((cwd / '.qwen/settings.json').read_text())
                        self.assertIn('model', settings)
                    self.assertNotIn('Skill instructions', command[-1])
                    return subprocess.CompletedProcess(command, 0, 'Done', '')
                with patch.dict(os.environ, {**env, 'CODING_CLI_RUNTIME':'local'}), \
                     patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/' + cli), \
                     patch('orchestrator.runtime.cli.subprocess.run', side_effect=run):
                    self.assertEqual(run_llm_cli('Task', cli, cwd=workspace, base_url='http://internal/v1', model='test'), 'Done')
            self.assertTrue(all(not path.exists() for path in observed))
            self.assertEqual(list(workspace.iterdir()), [])

    def test_disabled_paths_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'qwen.yaml'
            config.write_text('skills:\n  enabled: false\n  paths: [missing]\n')
            self.assertEqual(configured_skills('qwen', config), [])

    def test_duplicate_names_and_invalid_skill_metadata_fail(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            skill(root / 'first/sample-skill')
            skill(root / 'second/sample-skill')
            config = root / 'qwen.yaml'
            config.write_text('skills:\n  paths: [first, second]\n')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                configured_skills('qwen', config)
            config.write_text('skills:\n  paths: [first/sample-skill]\n')
            (root / 'first/sample-skill/SKILL.md').write_text('Missing frontmatter')
            with self.assertRaisesRegex(ValueError, 'frontmatter'):
                configured_skills('qwen', config)

    def test_docker_runner_receives_copied_skills_in_mounted_scratch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            skill(root / 'sample-skill')
            config = root / 'opencode.yaml'
            config.write_text('skills:\n  paths: [sample-skill]\n')
            def run(command, **kwargs):
                self.assertTrue((kwargs['workdir'] / '.opencode/skills/sample-skill/helper.py').exists())
                return subprocess.CompletedProcess(command, 0, 'Done', '')
            with patch.dict(os.environ, {'OPENCODE_SKILLS_CONFIG':str(config), 'CODING_CLI_RUNTIME':'docker'}), \
                 patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/docker'), \
                 patch('orchestrator.runtime.cli.run_in_docker', side_effect=run):
                self.assertEqual(run_llm_cli('Task', 'opencode', cwd=root, base_url='http://internal/v1', model='test'), 'Done')

    def test_repository_role_selections_are_isolated_for_both_backends(self):
        expected = {
            'plan': {'repository-discovery', 'dependency-planning', 'replan-recovery'},
            'execute': {'scoped-implementation', 'systematic-debugging', 'verification-before-completion', 'ponytail'},
            'reviewer': {'evidence-based-review', 'integration-regression', 'verification-before-completion'},
        }
        configs = Path(__file__).resolve().parents[1] / 'config/cli'
        for cli in ('qwen', 'opencode'):
            for role, names in expected.items():
                with self.subTest(cli=cli, role=role):
                    self.assertEqual({p.name for p in configured_skills(cli, configs / f'{cli}.yaml', role=role)}, names)
            self.assertEqual(configured_skills(cli, configs / f'{cli}.yaml'), [])

    def test_common_and_role_paths_merge_without_duplicate_skills(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            skill(root / 'sample-skill')
            config = root / 'qwen.yaml'
            config.write_text('skills:\n  paths: [sample-skill]\n  roles:\n    execute: [sample-skill]\n')
            self.assertEqual(len(configured_skills('qwen', config, role='execute')), 1)
            with self.assertRaisesRegex(ValueError, 'Skill role'):
                configured_skills('qwen', config, role='unknown')
            config.write_text('skills:\n  roles:\n    execute: bad-scalar\n')
            with self.assertRaisesRegex(ValueError, 'list of nonempty paths'):
                configured_skills('qwen', config)

    def test_runner_stages_only_the_selected_agent_skills(self):
        config = Path(__file__).resolve().parents[1] / 'config/cli/qwen.yaml'
        with tempfile.TemporaryDirectory() as workspace:
            def run(command, **kwargs):
                names = {p.name for p in (Path(kwargs['cwd']) / '.qwen/skills').iterdir()}
                self.assertIn('ponytail', names)
                self.assertIn('scoped-implementation', names)
                self.assertNotIn('repository-discovery', names)
                self.assertNotIn('evidence-based-review', names)
                self.assertNotIn('Lazy senior dev', command[-1])
                return subprocess.CompletedProcess(command, 0, 'Done', '')
            with patch.dict(os.environ, {'QWEN_SKILLS_CONFIG':str(config), 'CODING_CLI_RUNTIME':'local'}), \
                 patch('orchestrator.runtime.cli.shutil.which', return_value='/bin/qwen'), \
                 patch('orchestrator.runtime.cli.subprocess.run', side_effect=run):
                self.assertEqual(run_llm_cli('Implement task', role='execute', cwd=workspace,
                    base_url='http://internal/v1', model='test'), 'Done')
