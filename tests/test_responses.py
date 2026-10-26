import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, HumanMessage
from orchestrator.runtime.artifacts import (ARTIFACT_KEY, ARTIFACTS_KEY, RESPONSE_KEY,
    attach_artifacts, plan_artifact, plan_handoff_artifact, preprocess_messages)
from orchestrator.runtime.responses import (arewrite_update, rewrite_update,
    response_scope, response_settings)
from orchestrator.schemas import PlanGraph


def plan():
    return PlanGraph(nodes=[dict(id='a', instructions='Implement account page', context='Verified',
        related_files=[], acceptance_criteria=['Checks pass'], constraints=[])], edges=[])


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model = Mock()
        self.model.invoke.return_value = AIMessage(content='Mình chưa thể tiếp tục vì thiếu thông tin đăng nhập cần hỗ trợ.')
        self.model.ainvoke = AsyncMock(return_value=self.model.invoke.return_value)
        self.settings = response_settings()
        mocked = patch('orchestrator.runtime.responses.build_response_model', return_value=(self.model, self.settings))
        self.factory = mocked.start()
        self.addCleanup(mocked.stop)

    async def test_extra_call_uses_filtered_history_and_preserves_business_state_and_id(self):
        state = {'messages':[HumanMessage(content='Tôi không rành kỹ thuật, giải thích đơn giản nhé'),
                             plan_handoff_artifact(), plan_artifact(plan())], 'output':'Missing test prerequisite'}
        original = AIMessage(id='progress:execute:a', name='progress', content='Execute failed')
        update = {'status':'failed', 'error':'Missing test prerequisite', 'messages':[original]}
        result = await arewrite_update(state, update, {}, agent='execute', node='announce_result')
        self.model.ainvoke.assert_awaited_once()
        inputs = self.model.ainvoke.call_args.args[0]
        serialized = json.dumps([m.model_dump() for m in inputs], ensure_ascii=False)
        self.assertIn('không rành kỹ thuật', serialized)
        self.assertNotIn('flowchart TD', serialized)
        self.assertNotIn('plan_handoff', serialized)
        self.assertIn('no_stream', self.model.ainvoke.call_args.kwargs['config']['tags'])
        self.assertEqual(result['status'], update['status'])
        self.assertEqual(result['error'], update['error'])
        self.assertEqual(result['messages'][0].id, original.id)
        self.assertEqual(result['messages'][0].name, original.name)
        self.assertEqual(result['messages'][0].content, self.model.invoke.return_value.content)
        self.assertEqual(original.content, 'Execute failed')

    async def test_artifact_appended_after_model_and_preprocessing_retains_only_prose(self):
        artifact = plan_artifact(plan())
        update = {'status':'planned', 'messages':[AIMessage(id='summary', name='plan', content='Plan ready'), artifact]}
        result = await arewrite_update({'messages':[]}, update, {}, agent='plan', node='structure')
        message = result['messages'][0]
        self.assertEqual(len(result['messages']), 1)
        self.assertIn(artifact.content, message.content)
        self.assertEqual(message.additional_kwargs[ARTIFACTS_KEY][0]['content'], artifact.content)
        self.assertNotIn('flowchart TD', str(self.model.ainvoke.call_args))
        clean = preprocess_messages([message])
        self.assertEqual(clean[0].content, self.model.invoke.return_value.content)
        self.assertNotIn(ARTIFACTS_KEY, clean[0].additional_kwargs)
        self.assertNotIn(RESPONSE_KEY, clean[0].additional_kwargs)
        self.assertEqual(preprocess_messages(clean), clean)
        self.assertIn('flowchart TD', message.content)

    async def test_nested_nodes_do_not_rewrite_twice_and_artifact_only_needs_no_model(self):
        artifact = plan_handoff_artifact()
        result = await arewrite_update({}, {'messages':[artifact]}, {}, agent='team', node='announce_plan')
        self.assertEqual(result['messages'], [artifact])
        self.factory.assert_not_called()
        first = await arewrite_update({}, {'messages':[AIMessage(content='Done')]}, {}, agent='execute', node='coding')
        again = await arewrite_update({}, first, {}, agent='network', node='a')
        self.assertEqual(again, first)
        self.model.ainvoke.assert_awaited_once()

    async def test_all_artifact_types_removed_and_existing_mixed_message_rewritten_safely(self):
        other = AIMessage(content='opaque deterministic data', additional_kwargs={ARTIFACT_KEY:{'type':'future_artifact'}})
        self.assertEqual(preprocess_messages([other]), [])
        mixed = attach_artifacts(AIMessage(content='Old wording'), [other])
        result = await arewrite_update({'messages':[mixed]}, {'messages':[mixed]}, {}, agent='team', node='result')
        self.assertNotIn('opaque deterministic data', str(self.model.ainvoke.call_args))
        self.assertEqual(preprocess_messages(result['messages'])[0].content, self.model.invoke.return_value.content)
        self.assertIn('opaque deterministic data', result['messages'][0].content)

    async def test_presentation_failure_does_not_turn_successful_merge_into_failure_or_emit_raw(self):
        self.model.ainvoke.side_effect = RuntimeError('Provider unavailable')
        artifact = plan_artifact(plan())
        result = await arewrite_update({}, {'status':'completed', 'results':{'a':{'status':'completed',
            'integration':{'status':'merged'}}}, 'messages':[AIMessage(content='private raw trace'), artifact]}, {},
            agent='team', node='summarize_execution')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['results']['a']['integration']['status'], 'merged')
        self.assertEqual(result['messages'], [artifact])
        self.assertTrue(result['response_error'])
        self.assertNotIn('private raw trace', str(result['messages']))

    def test_sync_call_redacts_explicit_api_key_from_source_and_output(self):
        self.model.invoke.return_value = AIMessage(content='Summary secret-value')
        with response_scope({'api_key':'secret-value'}):
            result = rewrite_update({}, {'messages':[AIMessage(content='Draft secret-value')]}, {}, agent='plan', node='coding')
        self.model.invoke.assert_called_once()
        self.assertNotIn('secret-value', str(self.model.invoke.call_args))
        self.assertNotIn('secret-value', result['messages'][0].content)

    async def test_response_constructor_and_file_config_are_off_event_loop(self):
        try:
            from blockbuster import blockbuster_ctx
        except ImportError:
            self.skipTest('Blockbuster is installed with LangGraph dev')
        from orchestrator.runtime.logging import logged_node
        def construct(options):
            Path.cwd()
            return self.model, response_settings()
        self.factory.side_effect = construct
        async def action(state):
            return {'messages':[AIMessage(content='Draft')], 'status':'completed'}
        with blockbuster_ctx(scanned_modules=['orchestrator']):
            result = await logged_node('team','result',action).ainvoke({'messages':[]})
        self.assertEqual(result['status'], 'completed')
        self.model.ainvoke.assert_awaited_once()

    def test_filtered_agent_history_does_not_replace_parent_artifacts(self):
        from orchestrator.runtime.network import message_delta
        mixed = attach_artifacts(AIMessage(id='plan-summary', content='A readable plan',
            additional_kwargs={RESPONSE_KEY:{'rewritten':True}}), [plan_artifact(plan())])
        agent_history = preprocess_messages([mixed])
        self.assertEqual(message_delta(agent_history, [mixed]), [])
        changed = AIMessage(id='plan-summary', content='A genuinely updated plan')
        self.assertEqual(message_delta([changed], [mixed]), [changed])

    async def test_raw_plan_report_stays_in_technical_payload_while_chat_is_rewritten(self):
        from orchestrator.agents.plan import build_plan_agent
        with tempfile.TemporaryDirectory() as root, \
             patch('orchestrator.agents.plan.run_llm_cli', return_value='RAW technical findings about src/accounts.py'), \
             patch('orchestrator.agents.plan.ChatOpenAI') as technical:
            structured = technical.return_value.with_structured_output.return_value
            structured.ainvoke = AsyncMock(return_value=plan())
            result = await build_plan_agent(base_url='http://internal/v1', model='test').ainvoke(
                {'workspace':root, 'messages':[('user','Make my account page work; I do not code')]})
        self.assertEqual(result['output'], 'RAW technical findings about src/accounts.py')
        payload = json.loads(structured.ainvoke.call_args.args[0][1].content)
        self.assertEqual(payload['natural_plan'], result['output'])
        self.assertNotIn(result['output'], str([m.content for m in result['messages']]))
        self.assertTrue(any(m.additional_kwargs.get(ARTIFACTS_KEY) for m in result['messages']))

    async def test_failure_preserves_artifact_already_attached_to_unrewritten_message(self):
        self.model.ainvoke.side_effect = TimeoutError()
        mixed = attach_artifacts(AIMessage(content='Raw source'), [plan_artifact(plan())])
        result = await arewrite_update({}, {'status':'planned', 'messages':[mixed]}, {},
                                       agent='plan', node='structure')
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(len(result['messages']), 1)
        self.assertIn('flowchart TD', result['messages'][0].content)
        self.assertNotIn('Raw source', result['messages'][0].content)
        self.assertEqual(preprocess_messages(result['messages']), [])
