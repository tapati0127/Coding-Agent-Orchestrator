import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph import build_graph
from orchestrator.schemas import ClarificationDecision, PlanApprovalDecision
from tests.test_graph import plan


class PlanApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)
        env = patch.dict('os.environ', {'GIT_TASK_ISOLATION': 'false'})
        env.start()
        self.addCleanup(env.stop)
        for attr, target in (
            ('initial', 'orchestrator.agents.initialize.ChatOpenAI'),
            ('plan_chat', 'orchestrator.agents.plan.ChatOpenAI'),
            ('cli', 'orchestrator.agents.plan.run_llm_cli'),
            ('approval', 'orchestrator.agents.plan_approval.ChatOpenAI'),
            ('executor_factory', 'graph.build_execute_agent'),
            ('reviewer_factory', 'graph.build_reviewer_agent')):
            mocked = patch(target)
            setattr(self, attr, mocked.start())
            self.addCleanup(mocked.stop)
        self.initial.return_value.ainvoke = AsyncMock(return_value=AIMessage(content='Ready'))
        self.initial.return_value.with_structured_output.return_value.ainvoke = AsyncMock(
            return_value=ClarificationDecision(short_explanation='Clear', ready_for_plan=True))
        self.cli.return_value = 'RAW CLI findings and plan'
        self.plan_chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan(['a']))
        self.assessor = self.approval.return_value.with_structured_output.return_value
        self.assessor.ainvoke = AsyncMock(return_value=PlanApprovalDecision(
            short_explanation='Approved', ready_for_execute=True))
        completed = {'status': 'completed', 'output': 'Done', 'error': '',
            'assessment': {'status': 'pass', 'notes': [], 'explanation': 'Passed', 'blockers': []}}
        self.executor_factory.return_value.ainvoke = AsyncMock(return_value=completed)
        self.reviewer_factory.return_value.ainvoke = AsyncMock(return_value=completed)
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.config = {'configurable': {'thread_id': 'approval-test'}}
        self.workflow = build_graph(base_url='http://internal/v1', model='test', checkpointer=InMemorySaver())

    async def start(self):
        return await self.workflow.ainvoke({'workspace': self.workspace.name,
            'messages': [('user', 'Implement a')]}, self.config)

    async def test_interrupt_then_approve_does_not_repeat_cli_or_initialize(self):
        result = await self.start()
        self.assertEqual(result['__interrupt__'][0].value['type'], 'plan_approval')
        self.executor_factory.return_value.ainvoke.assert_not_awaited()
        self.assessor.ainvoke.assert_not_awaited()
        self.assertEqual(self.cli.call_count, 1)
        self.assertNotIn(self.cli.return_value, str([m.content for m in result['messages']]))
        artifacts = [m for m in result['messages'] if m.additional_kwargs.get('orchestrator_artifacts')]
        self.assertEqual(len(artifacts), 1)
        result = await self.workflow.ainvoke(Command(resume='Đồng ý'), self.config)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.cli.call_count, 1)
        self.initial.return_value.ainvoke.assert_awaited_once()
        self.assessor.ainvoke.assert_awaited_once()
        self.executor_factory.return_value.ainvoke.assert_awaited_once()
        self.assertEqual(result['plan_approval'], {'short_explanation': 'Approved', 'ready_for_execute': True})
        self.assertEqual([m.content for m in result['messages'] if m.type == 'human'], ['Implement a', 'Đồng ý'])

    async def test_revision_includes_feedback_and_previous_plan_and_asks_again(self):
        self.assessor.ainvoke.side_effect = [
            PlanApprovalDecision(short_explanation='Add tests', ready_for_execute=False),
            PlanApprovalDecision(short_explanation='Approved revision', ready_for_execute=True)]
        await self.start()
        result = await self.workflow.ainvoke(Command(resume='Thêm test API trước'), self.config)
        self.assertIn('__interrupt__', result)
        self.assertEqual(self.cli.call_count, 2)
        self.executor_factory.return_value.ainvoke.assert_not_awaited()
        self.assertIn('Thêm test API trước', self.cli.call_args.args[0])
        self.assertIn('previous_plan', self.cli.call_args.args[0])
        self.assertIn('Add tests', self.cli.call_args.args[0])
        attempts = [m.additional_kwargs['orchestrator_artifacts'][0]['attempt']
                    for m in result['messages'] if m.additional_kwargs.get('orchestrator_artifacts')]
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(result['plan_revision'], 1)
        self.assertEqual(result['replan_count'], 0)
        self.initial.return_value.ainvoke.assert_awaited_once()
        result = await self.workflow.ainvoke(Command(resume='Duyệt bản mới'), self.config)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.cli.call_count, 2)
        self.executor_factory.return_value.ainvoke.assert_awaited_once()

    async def test_assessment_failure_stops_without_execution(self):
        self.assessor.ainvoke.side_effect = RuntimeError('offline')
        await self.start()
        result = await self.workflow.ainvoke(Command(resume='Đồng ý'), self.config)
        self.assertEqual(result['status'], 'failed')
        self.executor_factory.return_value.ainvoke.assert_not_awaited()
        self.assertEqual(self.cli.call_count, 1)

    async def test_plan_only_calls_structured_model_and_no_response_rewriter(self):
        from orchestrator.agents.plan import build_plan_agent
        with patch('orchestrator.runtime.responses.build_response_model') as response:
            result = await build_plan_agent(base_url='http://internal/v1', model='test').ainvoke({
                'workspace': self.workspace.name, 'messages': [('user', 'Implement a')]})
        response.assert_not_called()
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(self.cli.call_count, 1)
        self.plan_chat.return_value.with_structured_output.return_value.ainvoke.assert_awaited_once()
        self.assertEqual(sum(m.name == 'plan' for m in result['messages']), 1)
