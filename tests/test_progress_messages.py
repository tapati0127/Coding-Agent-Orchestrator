import tempfile
from threading import Event
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from langgraph.graph.message import add_messages

from orchestrator.agents.execute import build_execute_agent
from orchestrator.runtime.network import message_delta
from orchestrator.schemas import Assessment


class MilestoneMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)

    async def test_start_arrives_before_cli_finishes_and_result_replaces_same_id(self):
        gate = Event()
        def coding(*args, **kwargs):
            if not gate.wait(2):
                raise RuntimeError('Start notification was not delivered')
            return 'Implemented and tested'
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.execute.run_llm_cli', side_effect=coding), patch('orchestrator.agents.execute.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=Assessment(
                    status='pass', notes=['Tests pass'], explanation='Task verified', blockers=[]))
                messages, updates = [], []
                async for update in build_execute_agent(base_url='http://internal/v1', model='coder').astream({
                    'workspace': workspace, 'task': {'task_id': 'api', 'instructions': 'Implement API'},
                }, stream_mode='updates'):
                    updates.append(update)
                    for node, fields in update.items():
                        messages = add_messages(messages, fields.get('messages', []))
                        if node == 'announce_start':
                            self.assertIn('bắt đầu', fields['messages'][0].content)
                            self.assertIn('(/terminals)', fields['messages'][0].content)
                            gate.set()
        names = [next(iter(update)) for update in updates]
        self.assertLess(names.index('announce_start'), names.index('coding'))
        self.assertLess(names.index('coding'), names.index('structure'))
        finished = next(u['coding'] for u in updates if 'coding' in u)['messages'][-1]
        self.assertIsInstance(finished, AIMessage)
        self.assertIn('đã chạy xong', finished.content)
        self.assertIn('(/terminals)', finished.content)
        self.assertLess(names.index('structure'), names.index('announce_result'))
        self.assertNotIn('messages', next(u['structure'] for u in updates if 'structure' in u))
        status_messages = [m for m in messages if m.id == 'progress:execute:api']
        self.assertEqual(len(status_messages), 1)
        self.assertIn('pass', status_messages[0].content)

    def test_delta_includes_changes_to_an_existing_message_id(self):
        before = AIMessage(id='team-progress', content='Running')
        after = AIMessage(id='team-progress', content='Completed')
        self.assertEqual(message_delta([before], [before]), [])
        self.assertEqual(message_delta([after], [before]), [after])
        self.assertEqual(len(add_messages([before], [after])), 1)

    async def test_full_task_network_only_calls_each_role_assessor_once(self):
        import json
        from orchestrator.agents.reviewer import build_reviewer_agent
        from orchestrator.runtime.network import build_execution_graph
        from tests.test_graph import plan
        result_assessment = Assessment(status='pass', notes=[], explanation='Checks passed', blockers=[])
        with tempfile.TemporaryDirectory() as workspace, \
             patch.dict('os.environ', {'GIT_TASK_ISOLATION': 'false'}), \
             patch('orchestrator.agents.execute.run_llm_cli', return_value='PRIVATE execute report') as execute_cli, \
             patch('orchestrator.agents.reviewer.run_llm_cli', return_value='PRIVATE review report') as review_cli, \
             patch('orchestrator.agents.execute.ChatOpenAI') as execute_chat, \
             patch('orchestrator.agents.reviewer.ChatOpenAI') as review_chat, \
             patch('orchestrator.runtime.responses.build_response_model') as response_model:
            for chat in (execute_chat, review_chat):
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=result_assessment)
            options = {'base_url': 'http://internal/v1', 'model': 'coder'}
            network = build_execution_graph(plan(['a']), build_execute_agent(**options),
                reviewer=build_reviewer_agent(**options))
            result = await network.ainvoke({'workspace': workspace, 'results': {},
                'messages': [('user', 'Implement a')]})
        response_model.assert_not_called()
        execute_cli.assert_called_once()
        review_cli.assert_called_once()
        for chat, report in ((execute_chat, 'PRIVATE execute report'), (review_chat, 'PRIVATE review report')):
            chat.assert_called_once()
            assessor = chat.return_value.with_structured_output.return_value.ainvoke
            assessor.assert_awaited_once()
            self.assertEqual(json.loads(assessor.call_args.args[0][1].content)['report'], report)
        self.assertEqual(result['results']['a']['status'], 'completed')
        self.assertEqual(result['results']['a']['output'], 'PRIVATE execute report')
        self.assertEqual(result['results']['a']['review']['output'], 'PRIVATE review report')
        chat_text = str([m.content for m in result['messages']])
        self.assertNotIn('PRIVATE', chat_text)
        self.assertIn('Chờ review tại mốc', chat_text)
        self.assertIn('đã duyệt nhóm', chat_text.lower())
