"""Real local Git repositories exercise branch isolation and review-gated merges."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.runtime.git_tasks import git
from orchestrator.runtime.network import build_execution_graph
from orchestrator.schemas import PlanGraph


def make_plan(ids, edges=()):
    return PlanGraph(nodes=[dict(id=key, instructions=f'Implement {key}', context='Verified repository',
        related_files=[], acceptance_criteria=['Checks pass'], constraints=[]) for key in ids],
        edges=[dict(source=a, target=b) for a, b in edges])


def passed():
    return {'status':'completed', 'output':'Verified', 'error':'',
        'assessment':{'status':'pass', 'notes':[], 'explanation':'Verified', 'blockers':[]}}


class GitTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.response_helpers import install_response_model
        install_response_model(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        (self.repo / 'shared.txt').write_text('original\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'Initial')
        self.initial = git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
        env = patch.dict(os.environ, {'GIT_TASK_ISOLATION':'true', 'GIT_TARGET_BRANCH':'main',
            'GIT_WORKTREE_ROOT':str(self.root / 'checkouts')})
        env.start()
        self.addCleanup(env.stop)

    async def run_plan(self, plan, execute, review):
        executor = AsyncMock()
        executor.ainvoke.side_effect = execute
        reviewer = AsyncMock()
        reviewer.ainvoke.side_effect = review
        result = await build_execution_graph(plan, executor, reviewer=reviewer).ainvoke(
            {'workspace':str(self.repo), 'messages':[], 'results':{}})
        return result, reviewer

    async def test_parallel_group_review_and_join_sees_merged_code(self):
        arrived = set()
        ready = asyncio.Event()
        execution_paths = []
        reviewed_files = []
        active_reviews = 0
        async def execute(state, config=None):
            key = state['task']['task_id']
            path = Path(state['workspace'])
            execution_paths.append(path)
            self.assertNotEqual(path, self.repo)
            if key in ('a', 'b'):
                arrived.add(key)
                if len(arrived) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 3)
                self.assertFalse((self.repo / f'{key}.txt').exists())
            else:
                self.assertTrue((path / 'a.txt').exists())
                self.assertTrue((path / 'b.txt').exists())
            (path / f'{key}.txt').write_text(key)
            return passed()
        async def review(state, config=None):
            nonlocal active_reviews
            active_reviews += 1
            self.assertEqual(active_reviews, 1)
            await asyncio.sleep(.03)
            active_reviews -= 1
            path = Path(state['workspace'])
            self.assertNotIn(path, execution_paths)
            reviewed_files.append({p.name for p in path.glob('*.txt')})
            for key in state['execution_result']['task_results']:
                self.assertTrue((path / f'{key}.txt').exists())
            return passed()
        result, _ = await self.run_plan(make_plan(('a','b','join'), [('a','join'),('b','join')]), execute, review)
        self.assertTrue(all(r['status'] == 'completed' for r in result['results'].values()))
        self.assertEqual(len(set(execution_paths)), 3)
        self.assertEqual(len(reviewed_files), 2)
        self.assertTrue({'a.txt','b.txt'} <= reviewed_files[0])
        self.assertTrue((self.repo / 'join.txt').exists())
        self.assertFalse(git(self.repo, 'status', '--porcelain').stdout.strip())
        self.assertTrue(all(not p.exists() for p in execution_paths))
        for entry in result['results'].values():
            self.assertEqual(entry['integration']['status'], 'merged')
            git(self.repo, 'merge-base', '--is-ancestor', entry['integration']['merged_commit'], 'main')

    async def test_chain_inherits_unreviewed_commits_and_merges_only_after_final_review(self):
        seen = []
        async def execute(state, config=None):
            key = state['task']['task_id']
            path = Path(state['workspace'])
            seen.append(path)
            self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)
            self.assertFalse((self.repo / 'a.txt').exists())
            if key == 'b':
                self.assertEqual((path / 'a.txt').read_text(), 'a')
            (path / f'{key}.txt').write_text(key)
            return passed()
        async def review(state, config=None):
            self.assertEqual(set(state['execution_result']['task_results']), {'a', 'b'})
            self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)
            for key in ('a', 'b'):
                self.assertTrue((Path(state['workspace']) / f'{key}.txt').exists())
            return passed()
        result, reviewer = await self.run_plan(make_plan(('a', 'b'), [('a', 'b')]), execute, review)
        reviewer.ainvoke.assert_awaited_once()
        self.assertEqual(len(set(seen)), 2)
        self.assertTrue(all(entry['status'] == 'completed' for entry in result['results'].values()))
        self.assertTrue(all(not path.exists() for path in seen))
        commits = {entry['integration']['merged_commit'] for entry in result['results'].values()}
        self.assertEqual(len(commits), 1)
        self.assertTrue((self.repo / 'a.txt').exists())
        self.assertTrue((self.repo / 'b.txt').exists())

    async def test_chain_failed_final_review_keeps_all_changes_off_main(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / (state['task']['task_id'] + '.txt')).write_text('candidate')
            return passed()
        async def review(state, config=None):
            return {'status': 'failed', 'output': 'Integration regression', 'error': 'Fix b',
                    'assessment': {'status': 'fail'}}
        result, reviewer = await self.run_plan(make_plan(('a', 'b'), [('a', 'b')]), execute, review)
        reviewer.ainvoke.assert_awaited_once()
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)
        for key in ('a', 'b'):
            entry = result['results'][key]
            self.assertEqual(entry['status'], 'failed')
            self.assertFalse((self.repo / f'{key}.txt').exists())
            self.assertTrue((Path(entry['integration']['worktree']) / f'{key}.txt').exists())
            self.assertEqual(entry['review']['task_ids'], ['a', 'b'])

    async def test_conflicting_tasks_do_not_overwrite_main_and_skip_descendant(self):
        arrived = set()
        ready = asyncio.Event()
        async def execute(state, config=None):
            key = state['task']['task_id']
            (Path(state['workspace']) / 'shared.txt').write_text(key + '\n')
            arrived.add(key)
            if len(arrived) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 3)
            return passed()
        async def review(state, config=None):
            return passed()
        result, reviewer = await self.run_plan(make_plan(('a','b','join'), [('a','join'),('b','join')]), execute, review)
        roots = [result['results'][key] for key in ('a', 'b')]
        self.assertTrue(all(r['status'] == 'failed' for r in roots))
        for failed in roots:
            self.assertIn('CONFLICT', failed['error'])
            self.assertTrue(Path(failed['integration']['worktree']).exists())
            self.assertTrue(Path(failed['integration']['group_worktree']).exists())
        self.assertEqual(result['results']['join']['status'], 'skipped')
        reviewer.ainvoke.assert_not_called()
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)
        self.assertEqual((self.repo / 'shared.txt').read_text(), 'original\n')
        self.assertFalse(git(self.repo, 'status', '--porcelain').stdout.strip())

    async def test_failed_review_retains_branch_without_modifying_main(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / 'change.txt').write_text('candidate')
            return passed()
        async def review(state, config=None):
            return {'status':'failed', 'output':'Regression', 'error':'Tests fail',
                    'assessment':{'status':'fail'}}
        result, _ = await self.run_plan(make_plan(('a',)), execute, review)
        entry = result['results']['a']
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)
        self.assertTrue(Path(entry['integration']['worktree']).exists())
        self.assertFalse((self.repo / 'change.txt').exists())

    async def test_dirty_workspace_fails_without_starting_executor(self):
        (self.repo / 'shared.txt').write_text('User changes\n')
        execute = AsyncMock(return_value=passed())
        result, _ = await self.run_plan(make_plan(('a',)), execute, AsyncMock(return_value=passed()))
        self.assertIn('must be clean', result['results']['a']['error'])
        execute.assert_not_called()
        self.assertEqual((self.repo / 'shared.txt').read_text(), 'User changes\n')

    async def test_reviewer_modification_is_not_merged(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / 'change.txt').write_text('candidate')
            return passed()
        async def review(state, config=None):
            (Path(state['workspace']) / 'shared.txt').write_text('Reviewer edited code')
            return passed()
        result, _ = await self.run_plan(make_plan(('a',)), execute, review)
        self.assertEqual(result['results']['a']['status'], 'failed')
        self.assertIn('Reviewer modified', result['results']['a']['error'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)

    async def test_main_advanced_during_review_requires_fresh_review(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / 'change.txt').write_text('candidate')
            return passed()
        async def review(state, config=None):
            # Simulate a separate Git client that does not honor our lock.
            (self.repo / 'external.txt').write_text('Keep external commit')
            git(self.repo, 'add', '.')
            git(self.repo, 'commit', '-m', 'External update')
            return passed()
        result, _ = await self.run_plan(make_plan(('a',)), execute, review)
        self.assertEqual(result['results']['a']['status'], 'failed')
        self.assertIn('Target branch changed', result['results']['a']['error'])
        self.assertFalse((self.repo / 'change.txt').exists())
        self.assertEqual((self.repo / 'external.txt').read_text(), 'Keep external commit')

    async def test_approved_merge_is_reused_without_starting_executor(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / 'change.txt').write_text('candidate')
            return passed()
        async def review(state, config=None):
            return passed()
        plan = make_plan(('a',))
        first, _ = await self.run_plan(plan, execute, review)
        executor, reviewer = AsyncMock(), AsyncMock()
        workflow = build_execution_graph(plan, executor, reviewer=reviewer,
            approved_work=[{'task':plan.nodes[0].model_dump(), 'result':first['results']['a']}])
        result = await workflow.ainvoke({'workspace':str(self.repo), 'messages':[], 'results':{}})
        self.assertTrue(result['results']['a']['reused'])
        executor.ainvoke.assert_not_called()
        reviewer.ainvoke.assert_not_called()

    async def test_failed_executor_keeps_uncommitted_work_without_review_or_merge(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / 'unfinished.txt').write_text('In progress')
            return {'status':'failed', 'output':'Blocked', 'error':'Tests fail'}
        result, reviewer = await self.run_plan(make_plan(('a',)), execute, AsyncMock(return_value=passed()))
        entry = result['results']['a']
        self.assertEqual(entry['status'], 'failed')
        reviewer.ainvoke.assert_not_called()
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD').stdout.strip(), self.initial)
        self.assertEqual((Path(entry['integration']['worktree']) / 'unfinished.txt').read_text(), 'In progress')

    async def test_git_isolation_and_approved_reuse_with_blockbuster_enabled(self):
        try:
            from blockbuster import blockbuster_ctx
        except ImportError:
            self.skipTest('Blockbuster is installed with LangGraph dev')
        async def execute(state, config=None):
            await asyncio.to_thread((Path(state['workspace']) / 'change.txt').write_text, 'candidate')
            return passed()
        async def review(state, config=None):
            return passed()
        executor, reviewer = AsyncMock(), AsyncMock()
        executor.ainvoke.side_effect = execute
        reviewer.ainvoke.side_effect = review
        plan = make_plan(('a',))
        workflow = await asyncio.to_thread(build_execution_graph, plan, executor, reviewer=reviewer)
        with blockbuster_ctx(scanned_modules=['orchestrator']):
            first = await workflow.ainvoke({'workspace':str(self.repo), 'messages':[], 'results':{}})
            second_graph = await asyncio.to_thread(build_execution_graph, plan, executor, reviewer=reviewer,
                approved_work=[{'task':plan.nodes[0].model_dump(), 'result':first['results']['a']}])
            second = await second_graph.ainvoke({'workspace':str(self.repo), 'messages':[], 'results':{}})
        self.assertEqual(first['results']['a']['status'], 'completed', first['results']['a'].get('error'))
        self.assertTrue(second['results']['a']['reused'])

    async def test_root_workflow_with_blockbuster_enabled(self):
        from tests.response_helpers import install_accepted_plan_feedback
        install_accepted_plan_feedback(self)
        try:
            from blockbuster import blockbuster_ctx
        except ImportError:
            self.skipTest('Blockbuster is installed with LangGraph dev')
        from langchain_core.runnables import RunnableLambda
        from graph import build_graph
        async def execute(state, config=None):
            await asyncio.to_thread((Path(state['workspace']) / 'change.txt').write_text, 'candidate')
            return passed()
        async def review(state, config=None):
            return passed()
        executor, reviewer = AsyncMock(), AsyncMock()
        executor.ainvoke.side_effect = execute
        reviewer.ainvoke.side_effect = review
        initializer = RunnableLambda(lambda state: {'workspace':str(self.repo),
            'status':'ready_for_plan', 'replan_count':0, 'approved_work':[],
            'execution_history':[], 'results':{}})
        planner = RunnableLambda(lambda state: {'plan':make_plan(('a',)).model_dump(),
            'natural_plan':'Implement a', 'status':'planned'})
        with patch('graph.build_initialize_agent', return_value=initializer), \
             patch('graph.build_plan_agent', return_value=planner), \
             patch('graph.build_execute_agent', return_value=executor), \
             patch('graph.build_reviewer_agent', return_value=reviewer):
            workflow = await asyncio.to_thread(build_graph)
            with blockbuster_ctx(scanned_modules=['orchestrator', 'graph']):
                result = await workflow.ainvoke({'messages':[('user','Implement a')]})
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(result['results']['a']['integration']['status'], 'merged')

    async def test_parallel_merges_need_no_response_model(self):
        async def execute(state, config=None):
            (Path(state['workspace']) / (state['task']['task_id'] + '.txt')).write_text('implemented')
            return passed()
        async def review(state, config=None):
            return passed()
        with patch('orchestrator.runtime.responses.build_response_model', side_effect=RuntimeError('Unavailable')) as response_model:
            result, _ = await self.run_plan(make_plan(('a','b')), execute, review)
        response_model.assert_not_called()
        self.assertFalse(result.get('response_error'))
        self.assertTrue(all(entry['status'] == 'completed' for entry in result['results'].values()))
        self.assertTrue(all(entry['integration']['status'] == 'merged' for entry in result['results'].values()))
        self.assertTrue((self.repo / 'a.txt').exists())
        self.assertTrue((self.repo / 'b.txt').exists())
