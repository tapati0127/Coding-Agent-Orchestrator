import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from graph import build_graph
from orchestrator.agents.initialize import build_initialize_agent
from orchestrator.schemas import ClarificationDecision


def decision(ready, missing=None):
    return ClarificationDecision(ready_for_plan=ready, summary='Implement requested API',
        missing_information=missing or [], explanation='Ready' if ready else 'Need API scope')


class InitializeAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)
        env = patch.dict(os.environ, {'QWEN_BASE_URL': 'http://internal/v1', 'QWEN_MODEL': 'coder'})
        env.start()
        self.addCleanup(env.stop)
        chat = patch('orchestrator.agents.initialize.ChatOpenAI')
        self.chat = chat.start()
        self.addCleanup(chat.stop)
        self.chat.return_value.ainvoke = AsyncMock(return_value=AIMessage(content='Which API endpoints should I add?'))
        self.chat.return_value.invoke.return_value = AIMessage(content='Which API endpoints should I add?')
        self.structure = self.chat.return_value.with_structured_output.return_value
        self.structure.ainvoke = AsyncMock(return_value=decision(False, ['API endpoints']))
        self.structure.invoke.return_value = decision(False, ['API endpoints'])

    async def test_unclear_stops_before_plan_without_workspace_argument(self):
        with tempfile.TemporaryDirectory() as workspace, patch.dict(os.environ, {'WORKSPACE_ROOT': workspace}), \
             patch('orchestrator.agents.plan.run_llm_cli') as cli:
            result = await build_graph().ainvoke({'messages': [('user', 'Add an API')]})
        self.assertEqual(result['workspace'], workspace)
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertFalse(result['clarification']['ready_for_plan'])
        self.assertEqual(len(result['messages']), 2)
        self.assertEqual(result['messages'][-1].name, 'initialize')
        cli.assert_not_called()
        self.chat.return_value.ainvoke.assert_called_once()
        self.structure.ainvoke.assert_called_once()
        natural_config = self.chat.return_value.ainvoke.call_args.kwargs['config']
        structured_config = self.structure.ainvoke.call_args.kwargs['config']
        self.assertIn('no_stream', natural_config.get('tags', []))
        self.assertIn('no_stream', structured_config['tags'])
        self.assertTrue(self.chat.call_args_list[0].kwargs['disable_streaming'])
        self.assertTrue(self.chat.call_args_list[1].kwargs['disable_streaming'])

    async def test_reply_then_user_answer_reaches_plan(self):
        from tests.test_graph import plan
        first_reply = 'Which API endpoints should I add?'
        self.chat.return_value.ainvoke.side_effect = [AIMessage(content=first_reply), AIMessage(content='I will hand the health endpoint task to Plan')]
        self.structure.ainvoke.side_effect = [decision(False, ['API endpoint']), decision(True)]
        with tempfile.TemporaryDirectory() as workspace, patch.dict(os.environ, {'WORKSPACE_ROOT': workspace}):
            workflow = build_graph(dispatch_execute=False)
            first = await workflow.ainvoke({'messages': [('user', 'Add an API')]})
            with patch('orchestrator.agents.plan.run_llm_cli', return_value='Implement GET /health') as cli, \
                 patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan(['health']))
                result = await build_graph(dispatch_execute=False).ainvoke({'messages': [*first['messages'], ('user', 'GET /health returns 200 JSON')]})
        self.assertEqual(result['status'], 'planned')
        self.assertEqual([m.name for m in result['messages'] if isinstance(m, AIMessage) and m.name != 'progress'], ['initialize', 'initialize', 'plan_handoff', 'plan', 'plan'])
        self.assertIn('GET /health returns 200 JSON', cli.call_args.args[0])
        payload = json.loads(self.structure.ainvoke.call_args.args[0][1].content)
        self.assertEqual(payload['messages'][-1]['data']['content'], 'I will hand the health endpoint task to Plan')

    async def test_structured_failure_emits_human_response_and_keeps_original_reply(self):
        self.structure.ainvoke.side_effect = RuntimeError('offline')
        with tempfile.TemporaryDirectory() as workspace, patch.dict(os.environ, {'WORKSPACE_ROOT': workspace}):
            updates = [update async for update in build_initialize_agent().astream({'messages': [('user', 'Task')]}, stream_mode='updates')]
        coding = next(update['coding'] for update in updates if 'coding' in update)
        structured = next(update['structure'] for update in updates if 'structure' in update)
        self.assertEqual(len(coding['messages']), 1)
        self.assertEqual(structured['messages'][0].name, 'initialize')
        self.assertTrue(structured['messages'][0].additional_kwargs['orchestrator_response']['rewritten'])
        self.assertEqual(structured['status'], 'failed')

    def test_sync_support_and_inconsistent_ready_is_rejected(self):
        self.structure.invoke.return_value = decision(True, ['Required contract'])
        with tempfile.TemporaryDirectory() as workspace, patch.dict(os.environ, {'WORKSPACE_ROOT': workspace}):
            result = build_initialize_agent().invoke({'messages': [('user', 'Task')]})
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertFalse(result['clarification']['ready_for_plan'])
        self.assertIn('no_stream', self.structure.invoke.call_args.kwargs['config']['tags'])

    async def test_natural_failure_does_not_call_structure(self):
        self.chat.return_value.ainvoke.side_effect = RuntimeError('offline')
        with tempfile.TemporaryDirectory() as workspace, patch.dict(os.environ, {'WORKSPACE_ROOT': workspace}):
            result = await build_initialize_agent().ainvoke({'messages': [('user', 'Task')]})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['messages']), 2)
        self.structure.ainvoke.assert_not_called()
