import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.schemas import ExecuteAssessment
from orchestrator.agents.reviewer import build_reviewer_agent


class ReviewerAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)

    async def test_cli_gets_messages_task_and_execution_report_then_assesses(self):
        for verdict in ('pass', 'fail'):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as workspace:
                with patch('orchestrator.agents.reviewer.run_llm_cli', return_value='Reviewed source and ran pytest: failure in auth.py') as cli:
                    with patch('orchestrator.agents.reviewer.ChatOpenAI') as chat:
                        chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=ExecuteAssessment(
                            status=verdict, notes=['auth.py test evidence'], explanation='Review complete', blockers=[]))
                        result = await build_reviewer_agent(base_url='http://internal/v1', model='coder').ainvoke({
                            'workspace': workspace, 'messages': [('user', 'Preserve login contract')],
                            'task': {'task_id': 'auth', 'instructions': 'Fix login', 'context': 'src/auth.py',
                                     'acceptance_criteria': ['Tests pass']},
                            'execution_result': {'status': 'completed', 'output': 'Execute says fixed',
                                                 'assessment': {'status': 'pass'}},
                        })
            prompt = cli.call_args.args[0]
            self.assertIn('Reviewer', prompt)
            self.assertIn('Preserve login contract', prompt)
            self.assertIn('Execute says fixed', prompt)
            self.assertIn('src/auth.py', prompt)
            self.assertEqual(result['assessment']['status'], verdict)
            self.assertEqual(result['messages'][-1].name, 'progress')
            self.assertIn('no_stream', chat.return_value.with_structured_output.return_value.ainvoke.call_args.kwargs['config']['tags'])
            self.assertEqual(result['status'], 'completed' if verdict == 'pass' else 'failed')
            chat.return_value.with_structured_output.return_value.ainvoke.assert_called_once()

    async def test_reviewer_failure_cannot_pass(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.reviewer.run_llm_cli', side_effect=RuntimeError('Review CLI failed')):
                with patch('orchestrator.agents.reviewer.ChatOpenAI') as chat:
                    chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=ExecuteAssessment(
                        status='pass', notes=[], explanation='Done', blockers=[]))
                    result = await build_reviewer_agent(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Task')],
                        'task': {'instructions': 'Fix API'}, 'execution_result': {'status': 'completed'},
                    })
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['assessment']['status'], 'fail')

    async def test_requires_messages_and_passed_execution(self):
        with tempfile.TemporaryDirectory() as workspace, patch('orchestrator.agents.reviewer.run_llm_cli') as cli:
            agent = build_reviewer_agent()
            for messages, status in (([], 'completed'), ([('user', 'Task')], 'failed')):
                with self.assertRaises(ValueError):
                    await agent.ainvoke({'workspace': workspace, 'messages': messages,
                        'task': {'instructions': 'Task'}, 'execution_result': {'status': status}})
            cli.assert_not_called()


if __name__ == '__main__':
    unittest.main()
