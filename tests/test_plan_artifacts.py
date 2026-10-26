import unittest
from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict
from orchestrator.runtime.artifacts import ARTIFACT_KEY, plan_artifact, plan_handoff_artifact, preprocess_messages
from orchestrator.schemas import PlanGraph


def make_plan():
    return PlanGraph(nodes=[dict(id=key, instructions='Implement ' + key + ' "quoted" | <tag>\n```',
        context='Internal findings', related_files=[], acceptance_criteria=['Tests pass'], constraints=[])
        for key in ('start', 'b', 'join')], edges=[dict(source=key, target='join') for key in ('start', 'b')])


class PlanArtifactTests(unittest.TestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)

    def test_render_parallel_join_and_safe_generated_ids(self):
        message = plan_artifact(make_plan(), attempt=2)
        self.assertEqual(message.additional_kwargs[ARTIFACT_KEY]['attempt'], 2)
        for text in ('```mermaid', 'start --> task_0', 'start --> task_1',
                     'task_0 --> task_2', 'task_1 --> task_2', 'task_2 --> finish',
                     'Tests pass', '&quot;', '&lt;tag&gt;', '&#124;'):
            self.assertIn(text, message.content)
        self.assertEqual(message.content.count('```'), 2)
        self.assertNotIn('Internal findings', message.content)

    def test_filter_is_nonmutating_idempotent_and_preserves_other_messages(self):
        human = HumanMessage(content='Keep user request')
        normal = AIMessage(content='Keep report')
        artifact = plan_artifact(make_plan())
        unrelated = AIMessage(content='Keep other attachment', additional_kwargs={'artifact': {'type':'other'}})
        messages = [human, plan_handoff_artifact(), artifact, normal, unrelated]
        before = messages_to_dict(messages)
        filtered = preprocess_messages(messages)
        self.assertEqual(filtered, [human, normal, unrelated])
        self.assertEqual(messages_to_dict(messages), before)
        self.assertEqual(preprocess_messages(filtered), filtered)

    def test_handoff_ids_and_replan_context(self):
        first = plan_handoff_artifact()
        second = plan_handoff_artifact(attempt=2, approved_count=3)
        self.assertTrue(first.id.startswith('plan-handoff:1:'))
        self.assertNotEqual(first.id, plan_handoff_artifact().id)
        self.assertIn('giữ 3 nhiệm vụ', second.content)
        self.assertIn('(/terminals)', first.content)
        self.assertEqual(preprocess_messages([first, second]), [])


class PlanHandoffStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)

    async def test_handoff_is_streamed_before_planner_runs(self):
        import asyncio
        from unittest.mock import patch
        from langchain_core.runnables import RunnableLambda
        from graph import build_graph
        gate = asyncio.Event()
        async def planner(state):
            await asyncio.wait_for(gate.wait(), timeout=2)
            self.assertTrue(any(m.name == 'plan_handoff' for m in state['messages']))
            return {'status': 'planned'}
        with patch('graph.build_initialize_agent', return_value=RunnableLambda(
                lambda state: {'status': 'ready_for_plan', 'replan_count': 0})), \
             patch('graph.build_plan_agent', return_value=RunnableLambda(planner)):
            nodes = []
            async for update in build_graph(dispatch_execute=False).astream(
                    {'messages': [HumanMessage(content='Implement a feature')]}, stream_mode='updates'):
                nodes.extend(update)
                if 'announce_plan' in update:
                    message = update['announce_plan']['messages'][0]
                    self.assertTrue(message.id.startswith('plan-handoff:1:'))
                    self.assertEqual(preprocess_messages([message]), [])
                    gate.set()
        self.assertLess(nodes.index('announce_plan'), nodes.index('plan'))
