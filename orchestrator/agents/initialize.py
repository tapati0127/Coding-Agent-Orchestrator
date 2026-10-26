"""Clarification entry graph: natural ChatOpenAI -> structured readiness gate."""
import asyncio
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage, messages_to_dict
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_openai import ChatOpenAI

from orchestrator.runtime.artifacts import preprocess_messages
from orchestrator.runtime.chat import build_chat_model
from orchestrator.runtime.thinking import thinking_options
from orchestrator.runtime.two_phase import CONFIG_ROOT, build_two_phase_graph, read_settings, workspace_path
from orchestrator.schemas import ClarificationDecision
from orchestrator.state import WorkflowState

DEFAULT_CONFIG = CONFIG_ROOT / 'initialize.yaml'


def build_initialize_agent(config_path: str | Path = DEFAULT_CONFIG, **llm_options):
    settings = read_settings(config_path)
    llm_options = thinking_options(settings, llm_options)
    timeout = settings.get('timeout', 120)

    def prepare(state):
        if not state.get('messages'):
            raise ValueError('messages must not be empty')
        return {'workspace': workspace_path(state.get('workspace')), 'output': '', 'cli_error': '',
            'prompt': settings['prompt'], 'clarification': {}, 'error': '', 'status': 'ready',
            'natural_plan': '', 'plan': {}, 'results': {}, 'replan_count': 0,
            'execution_history': [], 'approved_work': []}

    def model():
        return build_chat_model(llm_options, timeout, chat_factory=ChatOpenAI)

    def inputs(state):
        return [SystemMessage(content=settings['prompt']), *preprocess_messages(state['messages'])]

    def reply_update(reply):
        if not isinstance(reply, AIMessage) or not reply.content:
            raise ValueError('Initialize must return a natural AIMessage')
        reply = reply.model_copy(update={'name': 'initialize'})
        return {'messages': [reply], 'output': reply.content, 'cli_error': ''}

    def natural(state, config: RunnableConfig):
        try:
            return reply_update(model().invoke(inputs(state), config=config))
        except Exception:
            return {'output': '', 'cli_error': 'Initialize conversation failed',
                    'status': 'failed', 'error': 'Initialize conversation failed'}

    async def anatural(state, config: RunnableConfig):
        try:
            reply = await asyncio.wait_for(model().ainvoke(inputs(state), config=config), timeout=timeout)
            return reply_update(reply)
        except Exception:
            return {'output': '', 'cli_error': 'Initialize conversation failed',
                    'status': 'failed', 'error': 'Initialize conversation failed'}

    def payload(state):
        return {'messages': messages_to_dict(preprocess_messages(state['messages']))}

    def finish(state, decision):
        ready = decision.ready_for_plan and not decision.missing_information
        if not decision.summary.strip() or not decision.explanation.strip():
            raise ValueError('Empty clarification summary/explanation')
        return {'clarification': decision.model_copy(update={'ready_for_plan': ready}).model_dump(),
                'status': 'ready_for_plan' if ready else 'needs_clarification', 'error': ''}

    def failure(state, reason):
        return {'status': 'failed', 'error': reason, 'clarification': {}}

    return build_two_phase_graph(state_schema=WorkflowState, output_schema=ClarificationDecision,
        settings=settings, prepare=prepare, payload=payload, finish=finish, failure=failure,
        llm_options=llm_options, chat_factory=ChatOpenAI, stop_on_cli_error=True,
        natural_node=RunnableLambda(natural, afunc=anatural), agent_name='initialize')
