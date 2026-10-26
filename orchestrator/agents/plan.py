"""Plan role only: CLI discovery/natural plan -> validated structured DAG."""
import json
from pathlib import Path
from string import Template
from langchain_core.messages import AIMessage, messages_to_dict
from langchain_openai import ChatOpenAI
from orchestrator.runtime.artifacts import attach_artifacts, plan_artifact, preprocess_messages
from orchestrator.runtime.cli import run_llm_cli
from orchestrator.runtime.two_phase import CONFIG_ROOT, build_two_phase_graph, read_settings, workspace_path
from orchestrator.schemas import PlanGraph, validate_plan_graph
from orchestrator.state import PlanState

DEFAULT_CONFIG = CONFIG_ROOT / 'plan.yaml'


def build_plan_agent(config_path: str | Path = DEFAULT_CONFIG, **llm_options):
    """Return only the two-phase planning subgraph; orchestration is in graph.py."""
    settings = read_settings(config_path)

    def prepare(state):
        workspace = workspace_path(state.get('workspace'))
        if not state.get('messages'):
            raise ValueError('messages must not be empty')
        prompt = Template(settings['prompt']).substitute(workspace=workspace,
            messages=json.dumps(messages_to_dict(preprocess_messages(state['messages'])), ensure_ascii=False))
        if state.get('replan_count', 0):
            recovery = Template(settings['replan_prompt']).substitute(
                replan_count=state['replan_count'],
                execution_history=json.dumps(state.get('execution_history', []), ensure_ascii=False),
                approved_work=json.dumps(state.get('approved_work', []), ensure_ascii=False))
            prompt += '\n\n' + recovery
        if state.get('plan_approval', {}).get('ready_for_execute') is False:
            prompt += '\n\nKế hoạch trước chưa được người dùng duyệt. Cập nhật theo phản hồi mới nhất; không thực thi.\n'
            prompt += json.dumps({'previous_plan': state.get('plan', {}),
                'feedback': state.get('plan_feedback', ''),
                'assessment': state.get('plan_approval', {})}, ensure_ascii=False)
        return {'workspace': workspace, 'prompt': prompt, 'output': '', 'cli_error': '',
                'natural_plan': '', 'plan': {}, 'error': '', 'status': 'ready'}

    def payload(state):
        return {'workspace': state['workspace'], 'natural_plan': state['output'],
                'approved_work': state.get('approved_work', [])}

    def finish(state, plan):
        validate_plan_graph(plan)
        return {'natural_plan': state['output'], 'plan': plan.model_dump(),
                'status': 'planned', 'error': '',
                'messages': [attach_artifacts(AIMessage(content=f'Kế hoạch đã được kiểm tra, gồm {len(plan.nodes)} nhiệm vụ. Chưa thực thi các nhiệm vụ mới của kế hoạch lần này.', name='plan'),
                             [plan_artifact(plan, attempt=state.get('plan_revision', 0) + state.get('replan_count', 0) + 1)])]}

    def failure(state, reason):
        return {'natural_plan': state.get('output', ''), 'plan': {},
                'status': 'failed', 'error': f'Plan failed: {reason}'}

    return build_two_phase_graph(state_schema=PlanState, output_schema=PlanGraph,
        settings=settings, prepare=prepare, payload=payload, finish=finish, failure=failure,
        llm_options=llm_options, cli_runner=run_llm_cli, chat_factory=ChatOpenAI,
        stop_on_cli_error=True, agent_name='plan', rewrite_responses=False, compact_output=True)
