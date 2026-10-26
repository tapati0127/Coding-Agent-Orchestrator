"""Fake only the added response model; retain the real message/artifact pipeline."""
import json
from unittest.mock import patch
from langchain_core.messages import AIMessage
from orchestrator.runtime.responses import response_settings


class EchoResponseModel:
    def invoke(self, messages, config=None):
        return AIMessage(content=json.loads(messages[-1].content)['draft'])

    async def ainvoke(self, messages, config=None):
        return self.invoke(messages, config)


def install_response_model(test):
    mocked = patch('orchestrator.runtime.responses.build_response_model',
        side_effect=lambda options: (EchoResponseModel(), response_settings()))
    mocked.start()
    test.addCleanup(mocked.stop)


def install_accepted_plan_feedback(test):
    """Existing execution tests assume the user has approved every proposed plan."""
    from unittest.mock import AsyncMock, Mock
    from orchestrator.schemas import PlanApprovalDecision
    env = patch.dict('os.environ', {'QWEN_BASE_URL': 'http://internal/v1', 'QWEN_MODEL': 'test'})
    env.start()
    test.addCleanup(env.stop)
    feedback = patch('orchestrator.agents.plan_approval.interrupt', return_value='Đồng ý thực thi')
    feedback.start()
    test.addCleanup(feedback.stop)
    model = Mock()
    decision = PlanApprovalDecision(short_explanation='User approves this plan', ready_for_execute=True)
    model.with_structured_output.return_value.invoke.return_value = decision
    model.with_structured_output.return_value.ainvoke = AsyncMock(return_value=decision)
    factory = patch('orchestrator.agents.plan_approval.ChatOpenAI', return_value=model)
    factory.start()
    test.addCleanup(factory.stop)
