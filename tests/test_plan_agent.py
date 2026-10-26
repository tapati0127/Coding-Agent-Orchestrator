import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from langchain_core.runnables import RunnableLambda

from orchestrator.schemas import ExecuteAssessment

from orchestrator.schemas import PlanGraph, validate_plan_graph
from orchestrator.runtime.network import build_execution_graph
from graph import build_graph


def node(key):
    return dict(id=key, instructions='Implement ' + key, context='src/api.py verified findings',
                related_files=['src/api.py'], acceptance_criteria=['Tests pass'], constraints=[])


def plan(ids=('a',), edges=()):
    return PlanGraph.model_validate({'nodes': [node(key) for key in ids],
        'edges': [dict(source=a, target=b) for a, b in edges]})


class PlanAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)
        isolation = patch.dict('os.environ', {'GIT_TASK_ISOLATION': 'false'})
        isolation.start()
        self.addCleanup(isolation.stop)
        initializer = patch('graph.build_initialize_agent')
        factory = initializer.start()
        self.addCleanup(initializer.stop)
        factory.return_value = RunnableLambda(lambda state: {
            'workspace': state.get('workspace'), 'status': 'ready_for_plan', 'replan_count': 0,
            'execution_history': [], 'approved_work': [], 'results': {}, 'error': ''})
        reviewer = patch('graph.build_reviewer_agent')
        self.reviewer_factory = reviewer.start()
        self.addCleanup(reviewer.stop)
        self.reviewer = self.reviewer_factory.return_value
        self.reviewer.ainvoke = AsyncMock(return_value={
            'status': 'completed', 'output': 'Reviewed actual code and tests', 'error': '',
            'assessment': {'status': 'pass', 'notes': ['Tests pass'], 'explanation': 'Verified', 'blockers': []},
        })
        chat = patch('orchestrator.agents.execute.ChatOpenAI')
        self.execute_chat = chat.start()
        self.addCleanup(chat.stop)
        self.assessor = self.execute_chat.return_value.with_structured_output.return_value
        self.assessor.ainvoke = AsyncMock(return_value=ExecuteAssessment(
            status='pass', notes=['Tests passed'], explanation='Criteria met', blockers=[]))

    async def test_two_stages_and_context_handoff(self):
        natural = 'Explore src/api.py. Implement API then tests. Findings: FastAPI.'
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', return_value=natural) as cli, patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan(('a', 'b'), [('a', 'b')]))
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=['API implemented', 'Tests passed']) as execute:
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Implement ${api}')],
                    })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['natural_plan'], natural)
        self.assertEqual(set(result['plan']), {'nodes', 'edges'})
        self.assertIn('${api}', cli.call_args.args[0])
        self.assertEqual(cli.call_args.kwargs['task_title'], 'Implement ${api}')
        self.assertEqual(execute.call_args_list[0].kwargs['task_title'], 'Implement a')
        self.assertEqual(execute.call_args_list[0].kwargs['task_id'], 'a')
        chat.return_value.with_structured_output.assert_called_once_with(PlanGraph, method='json_schema')
        payload = chat.return_value.with_structured_output.return_value.ainvoke.call_args.args[0][1].content
        self.assertEqual(json.loads(payload)['natural_plan'], natural)
        self.assertIn('API implemented', execute.call_args_list[1].args[0])
        self.assertIn('verified findings', execute.call_args_list[0].args[0])
        from orchestrator.runtime.artifacts import ARTIFACT_KEY
        artifacts = [m for m in result['messages'] if any(a.get('type') == 'execution_plan' for a in m.additional_kwargs.get('orchestrator_artifacts', []))]
        self.assertEqual(len(artifacts), 1)
        self.assertNotIn('orchestrator_artifact', cli.call_args.args[0])
        self.assertNotIn('Bàn giao cho AI Plan', cli.call_args.args[0])
        self.assertIn('```mermaid', artifacts[0].content)
        for call in self.reviewer.ainvoke.call_args_list:
            self.assertTrue(all(ARTIFACT_KEY not in m.additional_kwargs for m in call.args[0]['messages']))
        for call in execute.call_args_list:
            self.assertNotIn('```mermaid', call.args[0])


    async def test_parallel_roots_and_unequal_branch_join_once(self):
        started, completed, calls = set(), set(), []
        both_roots = asyncio.Event()
        async def execute(value, config=None):
            key = value['task']['task_id']
            calls.append(key)
            started.add(key)
            if key in {'a', 'b'}:
                if {'a', 'b'} <= started:
                    both_roots.set()
                await asyncio.wait_for(both_roots.wait(), 2)
            if key == 'd':
                self.assertTrue({'a', 'c'} <= completed)
                self.assertIn('done c', value['task']['context'])
            completed.add(key)
            return dict(status='completed', output='done ' + key, error='')
        executor = AsyncMock()
        executor.ainvoke.side_effect = execute
        network = build_execution_graph(plan(('a', 'b', 'c', 'd'), [('b', 'c'), ('a', 'd'), ('c', 'd')]), executor)
        result = await network.ainvoke({'workspace': '.', 'results': {}})
        self.assertEqual(calls.count('d'), 1)
        self.assertEqual(set(result['results']), {'a', 'b', 'c', 'd'})

    async def test_failed_parent_skips_descendant_but_independent_branch_runs(self):
        async def execute(value, config=None):
            key = value['task']['task_id']
            return dict(status='failed' if key == 'a' else 'completed', output=key, error='')
        executor = AsyncMock()
        executor.ainvoke.side_effect = execute
        result = await build_execution_graph(plan(('a', 'b', 'c'), [('a', 'c')]), executor).ainvoke({'workspace': '.', 'results': {}})
        self.assertEqual(result['results']['c']['status'], 'skipped')
        self.assertEqual(result['results']['b']['status'], 'completed')
        self.assertEqual(executor.ainvoke.call_count, 2)

    async def test_invalid_structure_never_executes(self):
        invalid = plan(('a', 'b'), [('a', 'b'), ('b', 'a')])
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', return_value='Natural plan'), patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=invalid)
                with patch('orchestrator.agents.execute.run_llm_cli') as execute:
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({'workspace': workspace, 'messages': [('user', 'Task')]})
        self.assertEqual(result['status'], 'failed')
        execute.assert_not_called()

    async def test_plan_only_and_structure_failure(self):
        for failure in (False, True):
            with tempfile.TemporaryDirectory() as workspace:
                with patch('orchestrator.agents.plan.run_llm_cli', return_value='Natural plan'), patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                    chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan(), side_effect=RuntimeError('offline') if failure else None)
                    with patch('orchestrator.agents.execute.run_llm_cli') as execute:
                        result = await build_graph(dispatch_execute=False, base_url='http://internal/v1', model='coder').ainvoke({'workspace': workspace, 'messages': [('user', 'Task')]})
                self.assertEqual(result['status'], 'failed' if failure else 'planned')
                execute.assert_not_called()

    async def test_replans_failed_graph_using_history_then_succeeds(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', side_effect=['Initial plan', 'Repair plan']) as planner, patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=[
                    plan(('a', 'b'), [('a', 'b')]), plan(('repair',)),
                ])
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=[RuntimeError('build failed'), 'Fixed']) as execute:
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Implement API')],
                    })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['replan_count'], 1)
        self.assertEqual(len(result['execution_history']), 2)
        self.assertEqual(result['execution_history'][0]['results']['b']['status'], 'skipped')
        recovery_prompt = planner.call_args_list[1].args[0]
        self.assertIn('build failed', recovery_prompt)
        self.assertIn('Initial plan', recovery_prompt)
        self.assertIn('Implement API', recovery_prompt)
        self.assertEqual(set(result['results']), {'repair'})
        self.assertEqual(execute.call_count, 2)

    async def test_blocked_executor_replans_and_stops_at_budget(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', return_value='Recovery plan') as planner, patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan())
                with patch('graph.build_execute_agent') as factory:
                    factory.return_value.ainvoke = AsyncMock(return_value={
                        'status': 'blocked', 'output': 'Missing dependency', 'error': 'Cannot build',
                    })
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Task')],
                    })
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['replan_count'], 2)
        self.assertEqual(planner.call_count, 3)
        self.assertEqual(len(result['execution_history']), 3)
        self.assertIn('replan limit reached', result['error'])

    async def test_zero_budget_disables_replanning(self):
        from pathlib import Path
        import yaml
        with tempfile.TemporaryDirectory() as workspace:
            settings = yaml.safe_load(Path('config/plan.yaml').read_text())
            settings['max_replans'] = 0
            config_path = Path(workspace) / 'plan.yaml'
            config_path.write_text(yaml.safe_dump(settings))
            with patch('orchestrator.agents.plan.run_llm_cli', return_value='Plan') as planner, patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan())
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=RuntimeError('failed')):
                    result = await build_graph(config_path, base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Task')],
                    })
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['replan_count'], 0)
        self.assertEqual(planner.call_count, 1)

    async def test_semantic_fail_triggers_replan_with_assessment_notes(self):
        self.assessor.ainvoke.side_effect = [
            ExecuteAssessment(status='fail', notes=['src/api.py contract missing'],
                              explanation='Need API contract', blockers=['Contract unavailable']),
            ExecuteAssessment(status='pass', notes=['Verified report'], explanation='Done', blockers=[]),
        ]
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', side_effect=['Initial plan', 'Repair plan']) as planner, patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=[plan(), plan(('repair',))])
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=['Blocked on contract', 'Implemented and tested']):
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Implement API')],
                    })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['replan_count'], 1)
        self.assertEqual(result['execution_history'][0]['results']['a']['assessment']['status'], 'fail')
        recovery = planner.call_args_list[1].args[0]
        self.assertIn('src/api.py contract missing', recovery)
        self.assertIn('Need API contract', recovery)
        self.assertEqual(result['results']['repair']['assessment']['status'], 'pass')

    async def test_review_fail_replans_and_keeps_approved_work(self):
        pass_review = {'status': 'completed', 'output': 'a reviewed', 'error': '',
            'assessment': {'status': 'pass', 'notes': ['a verified'], 'explanation': 'Done', 'blockers': []}}
        fail_review = {'status': 'failed', 'output': 'b has regression', 'error': 'Regression found',
            'assessment': {'status': 'fail', 'notes': ['Fix b regression'], 'explanation': 'Bad behavior', 'blockers': []}}
        self.reviewer.ainvoke.side_effect = [pass_review, fail_review, pass_review]
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', side_effect=['Initial plan', 'Repair b only']) as planner, patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                # Replan accidentally includes an already approved task with a new ID.
                renamed = node('a')
                renamed['id'] = 'a_renamed'
                recovery = PlanGraph.model_validate({'nodes': [renamed, node('b')],
                    'edges': [{'source': 'a_renamed', 'target': 'b'}]})
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=[
                    plan(('a', 'b'), [('a', 'b')]), recovery])
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=['a implemented', 'b implemented', 'b repaired']) as execute:
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Keep public API unchanged')],
                    })
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['replan_count'], 1)
        self.assertEqual(execute.call_count, 3)  # approved a is not run again
        self.assertEqual(self.reviewer.ainvoke.call_count, 3)
        self.assertTrue(result['results']['a_renamed']['reused'])
        self.assertEqual(len(result['approved_work']), 2)
        prompt = planner.call_args_list[1].args[0]
        self.assertIn('Fix b regression', prompt)
        self.assertIn('a verified', prompt)
        review_input = self.reviewer.ainvoke.call_args_list[0].args[0]
        self.assertEqual(review_input['messages'][0].content, 'Keep public API unchanged')
        self.assertEqual(review_input['execution_result']['output'], 'a implemented')
        payload = chat.return_value.with_structured_output.return_value.ainvoke.call_args_list[1].args[0][1].content
        self.assertIn('a verified', payload)

    async def test_execute_fail_does_not_call_reviewer(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch('orchestrator.agents.plan.run_llm_cli', return_value='Plan'), patch('orchestrator.agents.plan.ChatOpenAI') as chat:
                chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan())
                with patch('orchestrator.agents.execute.run_llm_cli', side_effect=RuntimeError('build failed')):
                    result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke({
                        'workspace': workspace, 'messages': [('user', 'Task')],
                    })
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['approved_work'], [])
        self.reviewer.ainvoke.assert_not_called()

    async def test_reviewer_exception_is_failure_and_not_approved(self):
        self.reviewer.ainvoke.side_effect = RuntimeError('review offline')
        executor = AsyncMock()
        executor.ainvoke.return_value = {'status': 'completed', 'output': 'Implemented', 'error': '', 'assessment': {'status': 'pass'}}
        result = await build_execution_graph(plan(('a', 'b'), [('a', 'b')]), executor,
            reviewer=self.reviewer, messages=[('user', 'Task')]).ainvoke({'workspace': '.', 'results': {}})
        self.assertEqual(result['results']['a']['status'], 'failed')
        self.assertEqual(result['results']['b']['status'], 'skipped')
        self.assertEqual(executor.ainvoke.call_count, 1)

    def test_validate_graph(self):
        cases = [plan(('a', 'a')), plan(('a',), [('a', 'unknown')]),
                 plan(('a',), [('a', 'a')]), plan(('a', 'b'), [('a', 'b'), ('a', 'b')])]
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_plan_graph(invalid)

    async def test_empty_structured_plan_is_stop_signal_and_never_executes(self):
        empty = PlanGraph(nodes=[], edges=[])
        with self.assertRaisesRegex(ValueError, 'no executable tasks'):
            validate_plan_graph(empty)
        with tempfile.TemporaryDirectory() as workspace, \
             patch('orchestrator.agents.plan.run_llm_cli', return_value='Blocked: required API contract is unavailable'), \
             patch('orchestrator.agents.plan.ChatOpenAI') as chat, \
             patch('orchestrator.agents.execute.run_llm_cli') as execute:
            chat.return_value.with_structured_output.return_value.ainvoke = AsyncMock(return_value=empty)
            result = await build_graph(base_url='http://internal/v1', model='coder').ainvoke(
                {'workspace':workspace, 'messages':[('user','Implement unavailable API contract')]})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['plan'], {})
        execute.assert_not_called()
        self.assertFalse(any(m.additional_kwargs.get('orchestrator_artifact', {}).get('type') == 'execution_plan'
                             for m in result['messages']))


if __name__ == '__main__':
    unittest.main()
