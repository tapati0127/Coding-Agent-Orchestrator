"""Integration tests use actual role graphs and fake external boundaries only."""
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from langchain_core.messages import AIMessage
from orchestrator.schemas import ClarificationDecision

from graph import build_graph
from orchestrator.agents.plan import build_plan_agent
from orchestrator.schemas import Assessment, PlanGraph
from orchestrator.runtime.network import build_task_graph


def task(key):
    return {'id': key, 'instructions': 'Implement ' + key, 'context': 'Verified src/api.py',
            'related_files': ['src/api.py'], 'acceptance_criteria': ['Tests pass'], 'constraints': []}


def plan(ids):
    return PlanGraph.model_validate({'nodes': [task(key) for key in ids],
        'edges': [{'source': a, 'target': b} for a, b in zip(ids, ids[1:])]})


def assessment(verdict, reason):
    return Assessment(status=verdict, notes=[reason], explanation=reason, blockers=[])


class GraphIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        initializer = patch('orchestrator.agents.initialize.ChatOpenAI')
        chat = initializer.start()
        self.addCleanup(initializer.stop)
        chat.return_value.ainvoke = AsyncMock(return_value=AIMessage(content='Requirements are clear'))
        chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=ClarificationDecision(
            ready_for_plan=True, summary='Implement requested API', missing_information=[], explanation='Sufficient user details'))

    async def test_all_three_real_subgraphs_replan_and_preserve_approved_work(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', side_effect=['Initial natural plan', 'Repair b only']) as plan_cli, \
                 patch('orchestrator.agents.execute.run_llm_cli', side_effect=['Implemented a', 'Implemented b', 'Repaired b']) as execute_cli, \
                 patch('orchestrator.agents.reviewer.run_llm_cli', side_effect=['a checked', 'b has regression', 'b checked']) as review_cli, \
                 patch('orchestrator.agents.plan.ChatOpenAI') as plan_chat, \
                 patch('orchestrator.agents.execute.ChatOpenAI') as execute_chat, \
                 patch('orchestrator.agents.reviewer.ChatOpenAI') as review_chat:
                plan_chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=[plan(['a', 'b']), plan(['a', 'b'])])
                execute_chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=assessment('pass', 'Implemented and tested'))
                review_chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=[
                    assessment('pass', 'a approved'), assessment('fail', 'b regression requires repair'),
                    assessment('pass', 'b repair verified')])
                result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                    'workspace': workspace, 'messages': [('user', 'Implement API and preserve contracts')],
                })
        self.assertEqual(result['status'], 'completed')
        names = [message.name for message in result['messages']]
        self.assertEqual(names.count('initialize'), 1)
        self.assertEqual(names.count('plan'), 2)
        self.assertEqual(names.count('execute'), 3)
        self.assertEqual(names.count('reviewer'), 3)
        self.assertEqual(len(result['messages']), 16)
        self.assertEqual(sum(m.id == 'team-progress' for m in result['messages']), 1)
        self.assertIn('2/2', next(m.content for m in result['messages'] if m.id == 'team-progress'))
        self.assertEqual(result['replan_count'], 1)
        self.assertEqual(len(result['approved_work']), 2)
        self.assertEqual(plan_cli.call_count, 2)
        self.assertIn('no_stream', plan_chat.return_value.with_structured_output.return_value.ainvoke.call_args.kwargs['config']['tags'])
        self.assertEqual(execute_cli.call_count, 3)
        self.assertEqual(review_cli.call_count, 3)
        self.assertTrue(result['results']['a']['reused'])
        self.assertIn('b regression requires repair', plan_cli.call_args_list[1].args[0])
        self.assertIn('a approved', plan_cli.call_args_list[1].args[0])
        self.assertIn('preserve contracts', review_cli.call_args_list[0].args[0])
        self.assertEqual(result['execution_history'][0]['results']['b']['review']['assessment']['status'], 'fail')

    async def test_plan_subgraph_does_not_execute_tasks(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', return_value='Natural plan'), \
                 patch('orchestrator.agents.plan.ChatOpenAI') as chat, \
                 patch('orchestrator.agents.execute.run_llm_cli') as execute:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan(['a']))
                result = await build_plan_agent(base_url='http://internal/v1', model='coder').ainvoke({
                    'workspace': workspace, 'messages': [('user', 'Implement a')],
                })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['natural_plan'], 'Natural plan')
        execute.assert_not_called()

    async def test_plan_cli_failure_never_calls_structure(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', side_effect=RuntimeError('CLI failed')), \
                 patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                result = await build_plan_agent().ainvoke({'workspace': workspace, 'messages': [('user', 'Task')]})
        self.assertEqual(result['status'], 'failed')
        chat.assert_not_called()

    def test_shared_phase_and_composed_graph_topology(self):
        from orchestrator.agents.execute import build_execute_agent
        from orchestrator.agents.reviewer import build_reviewer_agent
        executor, reviewer = build_execute_agent(), build_reviewer_agent()
        for role in (build_plan_agent(), executor, reviewer):
            self.assertEqual(set(role.get_graph().nodes), {'__start__', 'prepare', 'announce_start', 'coding', 'structure', 'announce_result', '__end__'})
        task_graph = build_task_graph(executor, reviewer)
        self.assertIn('execute', task_graph.get_graph().nodes)
        self.assertIn('reviewer', task_graph.get_graph().nodes)
        outer = build_graph().get_graph()
        self.assertTrue(any('plan' in key for key in outer.nodes))
        self.assertIn('execute_network', outer.nodes)


if __name__ == '__main__':
    unittest.main()
