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
