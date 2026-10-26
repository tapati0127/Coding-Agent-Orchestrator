import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from orchestrator.runtime.logging import (LOGGER, configure_logging, endpoint_origin,
    event, logged_node, log_cli, operation, shutdown_logging)


class RuntimeLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)
        env = patch.dict(os.environ, {'ORCHESTRATOR_LOG_LEVEL':'INFO', 'CLI_LOG_DIR':'',
                                     'ORCHESTRATOR_LOG_FILE':''})
        env.start()
        self.addCleanup(env.stop)
        configure_logging()

    async def test_parallel_nodes_have_separate_context_and_do_not_dump_state(self):
        async def run(state):
            await asyncio.sleep(.01)
            event('llm.test', model='test-model')
            return {'status':'completed', 'output':'private-output', 'messages':[AIMessage(content='private-message')]}
        with self.assertLogs(LOGGER, level='INFO') as logs:
            await asyncio.gather(*(logged_node('execute', 'coding', run).ainvoke(
                {'task':{'task_id':key}, 'prompt':'private-prompt'},
                config={'configurable':{'thread_id':'thread-1'}}) for key in ('a','b')))
        text = '\n'.join(logs.output)
        self.assertEqual(text.count('event=node.start'), 2)
        self.assertEqual(text.count('event=node.end'), 2)
        for key in ('a','b'):
            self.assertTrue(any('event=llm.test' in line and f'task_id="{key}"' in line for line in logs.output))
        self.assertIn('thread_id="thread-1"', text)
        self.assertIn('elapsed_ms=', text)
        for secret in ('private-output','private-message','private-prompt'):
            self.assertNotIn(secret, text)

    def test_error_logs_type_without_echoing_exception_or_credentials(self):
        with patch.dict(os.environ, {'TEST_API_KEY':'sensitive-token'}), self.assertLogs(LOGGER, level='INFO') as logs:
            event('test', endpoint='sensitive-token')
            with self.assertRaises(RuntimeError):
                with operation('llm', model='test'):
                    raise RuntimeError('sensitive-token and private prompt')
        text = '\n'.join(logs.output)
        self.assertIn('[REDACTED]', text)
        self.assertIn('event=llm.error', text)
        self.assertIn('error_type="RuntimeError"', text)
        self.assertNotIn('sensitive-token', text)
        self.assertNotIn('private prompt', text)
        self.assertEqual(endpoint_origin('https://user:password@example.com/v1?token=secret'), 'https://example.com')

    def test_runtime_file_is_visible_to_monitor_without_duplicate_handlers(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CLI_LOG_DIR':root}):
            configure_logging()
            configure_logging()
            event('runtime.test')
            self.assertEqual(sum(isinstance(h, __import__('logging').handlers.QueueHandler) for h in LOGGER.handlers), 1)
            shutdown_logging()  # drain the queue before inspecting the file
            text = (Path(root) / 'orchestrator.log').read_text()
            self.assertEqual(text.count('event=runtime.test'), 1)
        configure_logging()

    async def test_structured_llm_and_node_log_status_after_invocation(self):
        from orchestrator.agents.plan import build_plan_agent
        from orchestrator.schemas import PlanGraph
        output = PlanGraph(nodes=[dict(id='a', instructions='Implement', context='Verified',
            related_files=[], acceptance_criteria=['Pass'], constraints=[])], edges=[])
        with tempfile.TemporaryDirectory() as workspace, \
             patch('orchestrator.agents.plan.run_llm_cli', return_value='private planning report'), \
             patch('orchestrator.agents.plan.ChatOpenAI') as chat:
            chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=output)
            with self.assertLogs(LOGGER, level='INFO') as logs:
                result = await build_plan_agent(base_url='http://internal/v1', model='test-model').ainvoke(
                    {'workspace':workspace, 'messages':[('user','private user request')]})
        self.assertEqual(result['status'], 'planned')
        text = '\n'.join(logs.output)
        self.assertIn('event=llm.start', text)
        self.assertIn('event=llm.end', text)
        self.assertIn('schema="PlanGraph"', text)
        self.assertIn('plan_nodes="1"', text)
        self.assertNotIn('private planning report', text)
        self.assertNotIn('private user request', text)

    def test_cli_logs_model_duration_and_output_size(self):
        @log_cli
        def fake(prompt, cli='qwen', **kwargs):
            return 'private result'
        with self.assertLogs(LOGGER, level='INFO') as logs:
            fake('private prompt', model='test', base_url='https://u:password@api.example/v1')
        text = '\n'.join(logs.output)
        for value in ('event=cli.start', 'event=cli.end', 'model="test"', 'output_chars="14"', 'elapsed_ms='):
            self.assertIn(value, text)
        self.assertNotIn('private result', text)
        self.assertNotIn('private prompt', text)
        self.assertNotIn('password', text)

    async def test_async_nodes_and_file_logging_with_blockbuster_enabled(self):
        try:
            from blockbuster import blockbuster_ctx
        except ImportError:
            self.skipTest('Blockbuster is installed with LangGraph dev')
        from orchestrator.agents.initialize import build_initialize_agent
        from orchestrator.schemas import ClarificationDecision
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CLI_LOG_DIR':root}):
            with patch('orchestrator.agents.initialize.ChatOpenAI') as chat:
                # Touch cwd in the synchronous constructor to guard model initialization too.
                instance = chat.return_value
                chat.side_effect = lambda **kwargs: (os.getcwd(), instance)[1]
                instance.ainvoke = AsyncMock(return_value=AIMessage(content='Which endpoint?'))
                instance.with_structured_output.return_value.ainvoke = AsyncMock(return_value=ClarificationDecision(
                    ready_for_plan=False, summary='Need endpoint', missing_information=['Endpoint'], explanation='Clarify'))
                workflow = await asyncio.to_thread(build_initialize_agent, base_url='http://internal/v1', model='test')
                with blockbuster_ctx(scanned_modules=['orchestrator']):
                    result = await workflow.ainvoke({'workspace':root, 'messages':[('user','Add API')]})
                    await asyncio.to_thread(shutdown_logging)
                self.assertEqual(result['status'], 'needs_clarification')
                text = (Path(root) / 'orchestrator.log').read_text()
                self.assertIn('event=node.end', text)
                self.assertIn('event=llm.end', text)
        configure_logging()
