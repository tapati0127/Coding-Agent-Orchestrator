"""Dependency review boundaries, independent branches and provisional results."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.runtime.network import build_execution_graph
from orchestrator.runtime.review_network import review_schedule
from orchestrator.schemas import PlanGraph


def plan(ids, edges=(), early=()):
    return PlanGraph(nodes=[{'id': key, 'instructions': 'Implement ' + key,
        'context': 'Verified source', 'related_files': [key + '.py'],
        'acceptance_criteria': ['Check ' + key], 'constraints': [], 'review_after': key in early}
        for key in ids], edges=[{'source': a, 'target': b} for a, b in edges])


def passed(output='Verified'):
    return {'status': 'completed', 'output': output, 'error': '',
            'assessment': {'status': 'pass', 'notes': [], 'explanation': 'Verified', 'blockers': []}}


class ReviewNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mocked = patch.dict('os.environ', {'GIT_TASK_ISOLATION': 'false'})
        mocked.start()
        self.addCleanup(mocked.stop)
        self.events, self.groups, self.review_inputs = [], [], []

    async def execute(self, state, config=None):
        key = state['task']['task_id']
        self.events.append('execute:' + key)
        return passed('Report ' + key)

    async def review(self, state, config=None):
        members = list(state['execution_result']['task_results'])
        self.groups.append(members)
        self.review_inputs.append(state)
        self.events.append('review:' + ','.join(members))
        return passed('Reviewed ' + ','.join(members))

    async def run_plan(self, dag, execute=None, review=None, approved_work=None):
        executor, reviewer = AsyncMock(), AsyncMock()
        executor.ainvoke.side_effect = execute or self.execute
        reviewer.ainvoke.side_effect = review or self.review
        result = await build_execution_graph(dag, executor, reviewer=reviewer,
            approved_work=approved_work).ainvoke({'workspace': '.', 'messages': [('user', 'Implement plan')], 'results': {}})
        return result, executor, reviewer

    async def test_chain_executes_all_before_one_final_review(self):
        result, _, reviewer = await self.run_plan(plan(('a', 'b', 'c'), [('a', 'b'), ('b', 'c')]))
        self.assertEqual(self.events, ['execute:a', 'execute:b', 'execute:c', 'review:a,b,c'])
        reviewer.ainvoke.assert_awaited_once()
        self.assertTrue(all(r['status'] == 'completed' for r in result['results'].values()))
        for entry in result['results'].values():
            self.assertEqual(entry['review']['task_ids'], ['a', 'b', 'c'])
        context = json.loads(self.review_inputs[0]['task']['context'])
        self.assertEqual(set(context['tasks']), {'a', 'b', 'c'})
        self.assertEqual(self.review_inputs[0]['task']['acceptance_criteria'], ['a: Check a', 'b: Check b', 'c: Check c'])

    async def test_unequal_parallel_branches_review_together_before_join(self):
        # Deliberately unordered nodes; the short branch must wait for the long one.
        dag = plan(('join', 'a', 'c', 'b'), [('a', 'b'), ('b', 'join'), ('c', 'join')])
        active, ready = set(), asyncio.Event()
        async def execute(state, config=None):
            key = state['task']['task_id']
            if key in ('a', 'c'):
                active.add(key)
                if len(active) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 2)
            return await self.execute(state, config)
        result, _, _ = await self.run_plan(dag, execute=execute)
        self.assertEqual([set(g) for g in self.groups], [{'a', 'b', 'c'}, {'join'}])
        first_review = next(i for i, event in enumerate(self.events) if event.startswith('review:'))
        self.assertLess(self.events.index('execute:b'), first_review)
        self.assertLess(first_review, self.events.index('execute:join'))
        self.assertTrue(all(r['status'] == 'completed' for r in result['results'].values()))

    async def test_failed_join_review_blocks_join_but_independent_chain_finishes(self):
        dag = plan(('a', 'b', 'join', 'x', 'y'), [('a', 'join'), ('b', 'join'), ('x', 'y')])
        async def review(state, config=None):
            result = await self.review(state, config)
            if set(state['execution_result']['task_results']) == {'a', 'b'}:
                return {'status': 'failed', 'output': 'a regression', 'error': 'Fix a regression',
                        'assessment': {'status': 'fail', 'notes': ['Fix a']}}
            return result
        result, executor, _ = await self.run_plan(dag, review=review)
        self.assertEqual(result['results']['join']['status'], 'skipped')
        self.assertEqual(result['results']['a']['status'], 'failed')
        self.assertEqual(result['results']['b']['status'], 'failed')
        self.assertEqual(result['results']['y']['status'], 'completed')
        self.assertEqual([set(g) for g in self.groups], [{'a', 'b'}, {'x', 'y'}])
        self.assertEqual(executor.ainvoke.await_count, 4)

    async def test_join_does_not_wait_for_unrelated_slow_branch(self):
        joined = asyncio.Event()
        dag = plan(('a', 'b', 'join', 'slow'), [('a', 'join'), ('b', 'join')])
        async def execute(state, config=None):
            if state['task']['task_id'] == 'slow':
                await asyncio.wait_for(joined.wait(), 2)
            if state['task']['task_id'] == 'join':
                joined.set()
            return await self.execute(state, config)
        result, _, _ = await self.run_plan(dag, execute=execute)
        self.assertTrue(all(r['status'] == 'completed' for r in result['results'].values()))
        self.assertEqual(set(self.groups[0]), {'a', 'b'})

    async def test_early_checkpoint_is_explicit_and_reviewed_only_once(self):
        dag = plan(('a', 'b', 'c'), [('a', 'b'), ('b', 'c')], early=('a',))
        await self.run_plan(dag)
        self.assertEqual(self.events, ['execute:a', 'review:a', 'execute:b', 'execute:c', 'review:b,c'])

    async def test_overlapping_joins_assign_shared_ancestor_to_one_review(self):
        dag = plan(('root', 'a', 'b', 'c', 'd', 'j1', 'j2'),
            [('root', 'a'), ('root', 'b'), ('a', 'j1'), ('c', 'j1'), ('b', 'j2'), ('d', 'j2')])
        result, _, _ = await self.run_plan(dag)
        reviewed = [key for group in self.groups for key in group]
        self.assertEqual(sorted(reviewed), sorted(n.id for n in dag.nodes))
        self.assertTrue(all(r['status'] == 'completed' for r in result['results'].values()))
        _, _, checkpoints, _, _ = review_schedule(dag)
        self.assertIn('__review_before_j1', checkpoints['__review_before_j2']['requires'])

    async def test_failed_execute_does_not_prevent_review_of_independent_success(self):
        async def execute(state, config=None):
            if state['task']['task_id'] == 'a':
                return {'status': 'failed', 'error': 'Build failed', 'output': 'Blocked'}
            return await self.execute(state, config)
        result, _, _ = await self.run_plan(plan(('a', 'b', 'join'), [('a', 'join'), ('b', 'join')]), execute=execute)
        self.assertEqual(self.groups, [['b']])
        self.assertEqual(result['results']['b']['status'], 'completed')
        self.assertEqual(result['results']['join']['status'], 'skipped')

    async def test_chain_review_failure_marks_whole_group_unapproved(self):
        async def review(state, config=None):
            await self.review(state, config)
            return {'status': 'failed', 'output': 'Regression', 'error': 'Fix group', 'assessment': {'status': 'fail'}}
        result, executor, reviewer = await self.run_plan(plan(('a', 'b'), [('a', 'b')]), review=review)
        self.assertEqual(executor.ainvoke.await_count, 2)
        reviewer.ainvoke.assert_awaited_once()
        self.assertTrue(all(r['status'] == 'failed' for r in result['results'].values()))

    async def test_custom_stream_reports_each_checkpoint_and_task(self):
        executor, reviewer = AsyncMock(), AsyncMock()
        executor.ainvoke.side_effect = self.execute
        reviewer.ainvoke.side_effect = self.review
        workflow = build_execution_graph(plan(('a', 'b'), [('a', 'b')]), executor, reviewer=reviewer)
        events = [event async for event in workflow.astream(
            {'workspace': '.', 'messages': [('user', 'Implement plan')], 'results': {}}, stream_mode='custom')]
        updates = [event for event in events if isinstance(event, dict) and event.get('node') in {'a', 'b', '__review_final'}]
        self.assertEqual([event['node'] for event in updates], ['a', 'b', '__review_final'])
        self.assertEqual(updates[0]['update']['results']['a']['status'], 'executed')
        self.assertEqual(updates[-1]['update']['results']['a']['status'], 'completed')

    async def test_checkpoint_resume_reuses_completed_execute_and_review_jobs(self):
        import sys
        if sys.version_info < (3, 11):
            self.skipTest('Async durable tasks require Python 3.11+')
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import StateGraph, START, END
        from orchestrator.runtime.network import ExecutionState
        checkpointed, hold_b = asyncio.Event(), asyncio.Event()
        attempts = {'a': 0, 'b': 0}
        async def execute(state, config=None):
            key = state['task']['task_id']
            attempts[key] += 1
            if key == 'b' and attempts[key] == 1:
                await hold_b.wait()
            return await self.execute(state, config)
        async def review(state, config=None):
            return await self.review(state, config)
        executor, reviewer = AsyncMock(), AsyncMock()
        executor.ainvoke.side_effect = execute
        reviewer.ainvoke.side_effect = review
        network = build_execution_graph(plan(('a', 'b'), early=('a',)), executor, reviewer=reviewer)
        graph = StateGraph(ExecutionState)
        graph.add_node('network', network)
        graph.add_edge(START, 'network')
        graph.add_edge('network', END)
        workflow = graph.compile(checkpointer=InMemorySaver())
        config = {'configurable': {'thread_id': 'resume-review-jobs'}}
        async def consume():
            async for namespace, event in workflow.astream(
                {'workspace': '.', 'messages': [('user', 'Implement')], 'results': {}}, config,
                stream_mode='custom', subgraphs=True):
                if event.get('node') == '__review_after_a':
                    checkpointed.set()
        runner = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(checkpointed.wait(), 3)
        finally:
            runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        result = await asyncio.wait_for(workflow.ainvoke(None, config), 3)
        self.assertEqual(attempts, {'a': 1, 'b': 2})
        self.assertEqual(self.groups, [['a'], ['b']])
        self.assertTrue(all(entry['status'] == 'completed' for entry in result['results'].values()))

    async def test_max_concurrency_limits_jobs_without_blocking_dependencies(self):
        active, peak = 0, 0
        async def execute(state, config=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            result = await self.execute(state, config)
            active -= 1
            return result
        executor, reviewer = AsyncMock(), AsyncMock()
        executor.ainvoke.side_effect = execute
        reviewer.ainvoke.side_effect = self.review
        workflow = build_execution_graph(plan(('join', 'a', 'b'), [('a', 'join'), ('b', 'join')]), executor, reviewer=reviewer)
        result = await asyncio.wait_for(workflow.ainvoke(
            {'workspace': '.', 'results': {}, 'messages': [('user', 'Implement')]}, {'max_concurrency': 1}), 3)
        self.assertEqual(peak, 1)
        self.assertTrue(all(entry['status'] == 'completed' for entry in result['results'].values()))
