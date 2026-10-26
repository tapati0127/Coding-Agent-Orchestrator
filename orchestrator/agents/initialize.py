"""Clarification entry graph: natural ChatOpenAI -> structured readiness gate."""
import asyncio
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage, messages_to_dict
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_openai import ChatOpenAI

from orchestrator.runtime.artifacts import preprocess_messages
from orchestrator.runtime.chat import build_chat_model, no_stream_config
from orchestrator.runtime.logging import operation, llm_fields
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
            'prompt': settings['prompt'], 'clarification': {}, 'error': '', 'status': 'ready', 'response_error': '',
            'natural_plan': '', 'plan': {}, 'results': {}, 'replan_count': 0,
            'plan_feedback': '', 'plan_approval': {}, 'plan_revision': 0,
            'execution_history': [], 'approved_work': []}

    def model():
        return build_chat_model(llm_options, timeout, chat_factory=ChatOpenAI, structured=True)

    def inputs(state):
        return [SystemMessage(content=settings['prompt']), *preprocess_messages(state['messages'])]

    def reply_update(reply):
        if not isinstance(reply, AIMessage) or not reply.content:
            raise ValueError('Initialize must return a natural AIMessage')
        reply = reply.model_copy(update={'name': 'initialize'})
        return {'messages': [reply], 'output': reply.content, 'cli_error': ''}

    def natural(state, config: RunnableConfig):
        try:
            with operation('llm', **llm_fields(llm_options, purpose='conversation'), timeout_s=timeout) as summary:
                reply = model().invoke(inputs(state), config=no_stream_config(config))
                update = reply_update(reply)
                summary['output_chars'] = len(str(reply.content))
                summary['token_usage'] = reply.usage_metadata
                return update
        except Exception:
            return {'output': '', 'cli_error': 'Initialize conversation failed',
                    'status': 'failed', 'error': 'Initialize conversation failed',
                    'messages': [AIMessage(content='Không lấy được phản hồi làm rõ yêu cầu. Chưa bàn giao cho Plan.', name='initialize')]}

    async def anatural(state, config: RunnableConfig):
        try:
            with operation('llm', **llm_fields(llm_options, purpose='conversation'), timeout_s=timeout) as summary:
                chat = await asyncio.to_thread(model)
                reply = await asyncio.wait_for(chat.ainvoke(inputs(state), config=no_stream_config(config)), timeout=timeout)
                update = reply_update(reply)
                summary['output_chars'] = len(str(reply.content))
                summary['token_usage'] = reply.usage_metadata
                return update
        except Exception:
            return {'output': '', 'cli_error': 'Initialize conversation failed',
                    'status': 'failed', 'error': 'Initialize conversation failed',
                    'messages': [AIMessage(content='Không lấy được phản hồi làm rõ yêu cầu. Chưa bàn giao cho Plan.', name='initialize')]}

    def payload(state):
        return {'messages': messages_to_dict(preprocess_messages(state['messages'])),
                'natural_response': state.get('output', '')}

    def finish(state, decision):
        ready = decision.ready_for_plan
        if not decision.short_explanation.strip():
            raise ValueError('Empty clarification short_explanation')
        return {'clarification': decision.model_dump(),
                'status': 'ready_for_plan' if ready else 'needs_clarification', 'error': ''}

    def failure(state, reason):
        return {'status': 'failed', 'error': reason, 'clarification': {},
                'messages': [AIMessage(content='Bước đánh giá yêu cầu chưa hoàn tất. Chưa bàn giao cho Plan. ' + reason, name='initialize')]}

    return build_two_phase_graph(state_schema=WorkflowState, output_schema=ClarificationDecision,
        settings=settings, prepare=prepare, payload=payload, finish=finish, failure=failure,
        llm_options=llm_options, chat_factory=ChatOpenAI, stop_on_cli_error=True,
        natural_node=RunnableLambda(natural, afunc=anatural), agent_name='initialize',
        rewrite_responses=False)
