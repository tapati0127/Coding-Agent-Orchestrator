"""Pause for a user's plan feedback, then assess it without repeating the CLI."""
import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt

from orchestrator.runtime.artifacts import preprocess_messages
from orchestrator.runtime.chat import build_chat_model, no_stream_config
from orchestrator.runtime.logging import logged_node, operation, llm_fields
from orchestrator.runtime.thinking import thinking_options
from orchestrator.runtime.two_phase import CONFIG_ROOT, read_settings
from orchestrator.schemas import PlanApprovalDecision


def request_plan_feedback(state):
    # Keep this node free of model/CLI side effects: LangGraph restarts it on resume.
    response = interrupt({
        'type': 'plan_approval',
        'question': 'Bạn có đồng ý thực thi kế hoạch này không? Hãy xác nhận hoặc nêu điều cần chỉnh sửa.',
        'plan': state['plan'],
    })
    if not isinstance(response, str) or not response.strip():
        raise ValueError('Plan feedback must be a nonempty string')
    return {'plan_feedback': response.strip(), 'plan_approval': {},
            'status': 'awaiting_plan_assessment',
            'messages': [HumanMessage(content=response.strip())]}


def build_plan_approval_agent(**llm_options):
    settings = read_settings(CONFIG_ROOT / 'plan_approval.yaml')
    options = thinking_options(settings, llm_options)
    timeout = settings['structure_timeout']

    def model():
        return build_chat_model(options, timeout, chat_factory=ChatOpenAI,
            structured=True).with_structured_output(PlanApprovalDecision,
                method=settings['structured_output_method'])

    def inputs(state):
        history = preprocess_messages(state.get('messages', []))
        payload = {'plan': state['plan'], 'feedback': state['plan_feedback'],
                   'history': [{'role': m.type, 'content': m.content} for m in history]}
        return [SystemMessage(content=settings['structure_prompt']),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False))]

    def finish(value):
        decision = PlanApprovalDecision.model_validate(value)
        if not decision.short_explanation.strip():
            raise ValueError('Empty approval short_explanation')
        return {'plan_approval': decision.model_dump(), 'error': '',
                'status': 'approved_for_execute' if decision.ready_for_execute else 'plan_revision_requested'}

    def failure():
        return {'status': 'failed', 'plan_approval': {},
                'error': 'Plan approval assessment failed; execution has not started'}

    def assess(state, config: RunnableConfig):
        try:
            with operation('llm', **llm_fields(options, purpose='plan_approval'), timeout_s=timeout):
                return finish(model().invoke(inputs(state), config=no_stream_config(config)))
        except Exception:
            return failure()

    async def aassess(state, config: RunnableConfig):
        try:
            with operation('llm', **llm_fields(options, purpose='plan_approval'), timeout_s=timeout):
                chat = await asyncio.to_thread(model)
                return finish(await asyncio.wait_for(
                    chat.ainvoke(inputs(state), config=no_stream_config(config)), timeout=timeout))
        except Exception:
            return failure()

    return logged_node('plan', 'assess_approval', RunnableLambda(assess, afunc=aassess),
                       response_options=options, rewrite_responses=False)
