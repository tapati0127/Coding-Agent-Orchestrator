import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestrator.runtime.chat import build_chat_model
from orchestrator.runtime.cli import run_llm_cli
from orchestrator.runtime.thinking import thinking_body, thinking_options
from orchestrator.runtime.two_phase import read_settings


class ThinkingTests(unittest.TestCase):
    def test_global_off_and_yaml_override(self):
        with patch.dict(os.environ, {'LLM_THINKING': 'false', 'LLM_THINKING_FORMAT': 'enable_thinking'}):
            self.assertFalse(thinking_options()['thinking'])
            self.assertTrue(thinking_options({'thinking': True})['thinking'])
            self.assertFalse(thinking_options({'thinking': False}, {'thinking': True})['thinking'])
            self.assertTrue(thinking_options({'thinking': None}, {'thinking': True})['thinking'])
        with patch.dict(os.environ, {'LLM_THINKING': 'true'}):
            self.assertFalse(thinking_options({'thinking': False})['thinking'])

    def test_explicit_request_formats(self):
        self.assertEqual(thinking_body(thinking=False), {'enable_thinking': False})
        self.assertEqual(thinking_body(thinking=True), {'enable_thinking': True})
        self.assertEqual(thinking_body(thinking=False, thinking_format='chat_template_kwargs'),
                         {'chat_template_kwargs': {'enable_thinking': False}})

    def test_chat_uses_global_or_role_policy_for_both_call_types(self):
        factory=Mock()
        with patch.dict(os.environ, {'LLM_THINKING':'false','LLM_THINKING_FORMAT':'enable_thinking'}):
            for structured in (False,True):
                build_chat_model({'base_url':'http://llm.internal/v1','model':'test'}, 30, chat_factory=factory, structured=structured)
                self.assertEqual(factory.call_args.kwargs['extra_body'], {'enable_thinking':False})
                build_chat_model(thinking_options({'thinking':True}, {'base_url':'http://llm.internal/v1','model':'test'}), 30, chat_factory=factory, structured=structured)
                self.assertEqual(factory.call_args.kwargs['extra_body'], {'enable_thinking':True})

    def test_cli_configs_include_effective_policy_and_get_cleaned(self):
        for cli in ('qwen','opencode'):
            for enabled in (False,True):
                with self.subTest(cli=cli,enabled=enabled), tempfile.TemporaryDirectory() as project:
                    seen=[]
                    def run(command,**kwargs):
                        workdir=Path(kwargs['cwd'])
                        seen.append(workdir)
                        if cli=='qwen':
                            config=json.loads((workdir/'.qwen/settings.json').read_text())
                            body=config['model']['generationConfig']['extra_body']
                        else:
                            config=json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                            body=config['provider']['internal']['models']['test']['options']
                        self.assertEqual(body, {'enable_thinking':enabled})
                        return subprocess.CompletedProcess(command,0,'Done','')
                    with patch.dict(os.environ, {'CODING_CLI_RUNTIME':'local','LLM_THINKING':'false','LLM_THINKING_FORMAT':'enable_thinking'}), patch('orchestrator.runtime.cli.shutil.which',return_value='/bin/'+cli), patch('orchestrator.runtime.cli.subprocess.run',side_effect=run):
                        run_llm_cli('Task',cli,cwd=project,base_url='http://llm.internal/v1',model='test',thinking=enabled)
                    self.assertFalse(seen[0].exists())

    def test_invalid_overrides_rejected(self):
        with self.assertRaises(ValueError):
            thinking_options({'thinking':'false'})
        with self.assertRaises(ValueError):
            thinking_options({'thinking_format':'unknown'})
        with patch.dict(os.environ,{'LLM_THINKING':'invalid'}), self.assertRaises(ValueError):
            thinking_options()
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'bad.yaml'
            path.write_text('prompt: hello\nstructure_prompt: convert\nthinking: "false"\n')
            with self.assertRaises(ValueError):
                read_settings(path)
