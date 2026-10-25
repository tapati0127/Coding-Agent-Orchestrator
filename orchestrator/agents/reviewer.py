"""Reviewer role: inspect code/tests with CLI, then assess its review report."""
import json
from pathlib import Path
from langchain_core.messages import messages_to_dict
from langchain_openai import ChatOpenAI
from orchestrator.runtime.cli import run_llm_cli
from orchestrator.runtime.two_phase import CONFIG_ROOT
from orchestrator.state import TaskState as ReviewerState
from .assessed import build_assessed_agent

DEFAULT_CONFIG = CONFIG_ROOT / 'reviewer.yaml'


def build_reviewer_agent(config_path: str | Path = DEFAULT_CONFIG, **llm_options):
    def enrich_task(state):
        if not state.get('messages'):
            raise ValueError('Reviewer messages must not be empty')
        result = state.get('execution_result', {})
        if result.get('status') != 'completed':
            raise ValueError('Only passed Execute results can be reviewed')
        task = dict(state['task'])
        task['context'] = json.dumps({
            'task_context': task.get('context', ''), 'related_files': task.get('related_files', []),
            'messages': messages_to_dict(state['messages']), 'execution_result': result,
        }, ensure_ascii=False)
        return task

    return build_assessed_agent(config_path, role='Reviewer', enrich_task=enrich_task,
        cli_runner=run_llm_cli, chat_factory=ChatOpenAI, **llm_options)
