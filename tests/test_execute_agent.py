import asyncio
import json
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.schemas import ExecuteAssessment
from orchestrator.agents.execute import build_execute_agent


def assessment(status='pass', blockers=None):
    return ExecuteAssessment(status=status, notes=['Checked report against task'],
                             explanation='Criteria met' if status == 'pass' else 'Required test failed',
                             blockers=blockers or [])


class ExecuteAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)
        env = patch.dict('os.environ', {'QWEN_BASE_URL': 'http://internal/v1', 'QWEN_MODEL': 'coder'})
        env.start()
        self.addCleanup(env.stop)
        chat = patch('orchestrator.agents.execute.ChatOpenAI')
        self.chat = chat.start()
        self.addCleanup(chat.stop)
        self.structured = self.chat.return_value.with_structured_output.return_value
        self.structured.invoke.return_value = assessment()
        self.structured.ainvoke = AsyncMock(return_value=assessment())

    async def test_task_and_report_reach_assessor(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.execute.run_llm_cli', return_value='Implemented; tests passed') as cli:
                result = await build_execute_agent().ainvoke({
                    'workspace': workspace,
                    'task': {'instructions': 'Preserve ${value} and {braces}',
                             'acceptance_criteria': ['API works', 'Tests pass']},
                })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['assessment']['status'], 'pass')
        self.assertEqual(result['output'], 'Implemented; tests passed')
        self.assertEqual(result['messages'][-1].name, 'progress')
        self.assertIn('no_stream', self.structured.ainvoke.call_args.kwargs['config']['tags'])
        self.assertIn('Preserve ${value} and {braces}', cli.call_args.args[0])
        self.assertEqual(cli.call_args.kwargs['task_title'], 'Preserve ${value} and {braces}')
        self.assertIn('- API works\n- Tests pass', cli.call_args.args[0])
        payload = json.loads(self.structured.ainvoke.call_args.args[0][1].content)
        self.assertEqual(payload['report'], result['output'])
        self.assertEqual(payload['task']['acceptance_criteria'], ['API works', 'Tests pass'])
        self.chat.return_value.with_structured_output.assert_called_once_with(ExecuteAssessment, method='json_schema')

    async def test_semantic_failure_even_when_cli_succeeds(self):
        self.structured.ainvoke.return_value = assessment('fail')
        with tempfile.TemporaryDirectory() as workspace, patch('orchestrator.agents.execute.run_llm_cli', return_value='Cannot finish: required test fails'):
            result = await build_execute_agent().ainvoke({'workspace': workspace, 'task': {'instructions': 'Implement API'}})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'Required test failed')
        self.assertTrue(result['assessment']['notes'])
        self.assertIn('Cannot finish', result['output'])

    async def test_blockers_cannot_pass(self):
        self.structured.ainvoke.return_value = assessment('pass', ['Missing API contract'])
        with tempfile.TemporaryDirectory() as workspace, patch('orchestrator.agents.execute.run_llm_cli', return_value='Partially implemented'):
            result = await build_execute_agent().ainvoke({'workspace': workspace, 'task': {'instructions': 'Implement API'}})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['assessment']['status'], 'fail')

    async def test_assessment_error_or_invalid_schema_fails_closed(self):
        for outcome in (RuntimeError('offline'), {'status': 'unknown'}, asyncio.TimeoutError()):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as workspace:
                self.structured.ainvoke.side_effect = outcome if isinstance(outcome, Exception) else None
                self.structured.ainvoke.return_value = outcome
                with patch('orchestrator.agents.execute.run_llm_cli', return_value='Natural report'):
                    result = await build_execute_agent().ainvoke({'workspace': workspace, 'task': {'instructions': 'Task'}})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['assessment']['status'], 'fail')
                self.assertEqual(result['output'], 'Natural report')

    async def test_cli_failures_cannot_be_overridden_by_assessor(self):
        for failure in (RuntimeError('CLI failed'), subprocess.TimeoutExpired('qwen', 1), None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as workspace:
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=failure, return_value=''):
                    result = await build_execute_agent().ainvoke({'workspace': workspace, 'task': {'instructions': 'Task'}})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['assessment']['status'], 'fail')
                self.assertTrue(result['error'])

    def test_sync_invoke_supported(self):
        with tempfile.TemporaryDirectory() as workspace, patch('orchestrator.agents.execute.run_llm_cli', return_value='Implemented; tests pass'):
            result = build_execute_agent().invoke({'workspace': workspace, 'task': {'instructions': 'Task'}})
        self.assertEqual(result['status'], 'completed')
        self.structured.invoke.assert_called_once()

    async def test_invalid_task_never_launches_cli_or_assessor(self):
        with tempfile.TemporaryDirectory() as workspace, patch('orchestrator.agents.execute.run_llm_cli') as cli:
            with self.assertRaisesRegex(ValueError, 'task.instructions'):
                await build_execute_agent().ainvoke({'workspace': workspace, 'task': {}})
        cli.assert_not_called()
        self.chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
