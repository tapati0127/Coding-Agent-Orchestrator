"""Execute on a DAG; review disjoint batches at joins and the final checkpoint."""
import asyncio
import json
import os
import sys

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.config import get_config
from langgraph.runtime import Runtime
from langgraph.func import task as durable_task

from .artifacts import preprocess_messages
from .git_tasks import (TaskCheckout, cleanup_execution_worktree, git_operation,
                        isolation_enabled, merged_commit_is_ancestor)
from .logging import event, logged_node
from .network import ExecutionState, build_task_graph, message_delta, task_signature
from .status import progress_message
from orchestrator.schemas import validate_plan_graph


FIELDS = ('status', 'output', 'error', 'assessment')


def review_passed(result):
    assessment = result.get('assessment')
    return (result.get('status') == 'completed' and isinstance(assessment, dict)
            and assessment.get('status') == 'pass' and not assessment.get('blockers'))


def review_schedule(plan):
    """Assign each task to one review, never review the same ancestor twice.

    A shared ancestor belongs to the first reachable checkpoint in topological
    order. Other checkpoints depend on its owner before reviewing descendants.
    """
    parents = validate_plan_graph(plan)
    order, ancestors, pending = [], {}, {n.id for n in plan.nodes}
    while pending:
        for node in plan.nodes:
            key = node.id
            if key in pending and all(p in ancestors for p in parents[key]):
                ancestors[key] = set(parents[key]).union(*(ancestors[p] for p in parents[key]))
                order.append(key)
                pending.remove(key)
    nodes = {n.id: n for n in plan.nodes}
    owners, checkpoints, before, after = {}, {}, {}, {}

    def checkpoint(name, scope, triggers):
        members = [key for key in order if key in scope and key not in owners]
        required = list(dict.fromkeys(owners[key] for key in order if key in scope and key in owners))
        checkpoints[name] = {'members': members, 'scope': scope,
                             'requires': required, 'triggers': triggers}
        owners.update({key: name for key in members})

    for key in order:
        if len(parents[key]) > 1:
            name = '__review_before_' + key
            checkpoint(name, ancestors[key], parents[key])
            before[key] = name
        if nodes[key].review_after:
            name = '__review_after_' + key
            checkpoint(name, ancestors[key] | {key}, [key])
            after[key] = name
    sinks = [key for key in order if not any(key in values for values in parents.values())]
    checkpoint('__review_final', set(order), sinks)
    return parents, ancestors, checkpoints, before, after


def review_task(group_id, members, nodes, entries):
    if len(members) == 1:
        task = nodes[members[0]].model_dump(exclude={'id'})
        task['task_id'] = members[0]
    else:
        task = {'task_id': group_id.lstrip('_'),
            'instructions': 'Kiểm tra tích hợp nhóm nhiệm vụ: ' + ', '.join(members),
            'acceptance_criteria': [f'{key}: {criterion}' for key in members
                                    for criterion in nodes[key].acceptance_criteria],
            'constraints': list(dict.fromkeys(value for key in members for value in nodes[key].constraints)),
            'related_files': list(dict.fromkeys(value for key in members for value in nodes[key].related_files))}
    task['context'] = json.dumps({'review_group': group_id,
        'tasks': {key: nodes[key].model_dump() for key in members},
        'execution_results': {key: entries[key] for key in members}}, ensure_ascii=False)
    return task


def build_review_network(plan, executor, *, reviewer=None, messages=None, approved_work=None):
    parents, ancestors, checkpoints, before, after = review_schedule(plan)
    nodes = {node.id: node for node in plan.nodes}
    isolated = isolation_enabled()
    approved = {task_signature(item['task']): item['result'] for item in (approved_work or [])}
    execution_graph = build_task_graph(executor)
    graph = StateGraph(ExecutionState)

    def execute_node(node):
        async def execute(state, config: RunnableConfig):
            prior = approved.get(task_signature(node.model_dump()))
            if prior is not None:
                if prior.get('status') != 'completed' or prior.get('review', {}).get('assessment', {}).get('status') != 'pass':
                    prior = None
                elif isolated:
                    integration = prior.get('integration', {})
                    if (integration.get('status') != 'merged' or not integration.get('merged_commit')
                            or not await git_operation(merged_commit_is_ancestor, state['workspace'],
                                integration['merged_commit'], os.getenv('GIT_TARGET_BRANCH') or 'main')):
                        prior = None
            if prior is not None:
                event('task.reused', task_id=node.id)
                return {'results': {node.id: {**prior, 'reused': True}}}
            dependencies = {key: state['results'][key] for key in parents[node.id]}
            if any(state['results'][key]['status'] not in {'executed', 'completed'} for key in ancestors[node.id]):
                event('task.skipped', task_id=node.id, reason='dependency_failed')
                return {'results': {node.id: {'status': 'skipped', 'output': '', 'error': 'Dependency failed'}}}
            checkout = None
            produced, response_error = [], ''
            entry = {'status': 'failed', 'output': '', 'error': ''}
            try:
                if isolated:
                    if reviewer is None:
                        raise ValueError('Isolated execution requires a Reviewer before merging')
                    checkout = await git_operation(TaskCheckout, state['workspace'], node.id)
                    await checkout.create([value['integration']['commit'] for value in dependencies.values()])
                task = node.model_dump(exclude={'id'})
                task.update(task_id=node.id, context=json.dumps({'findings': node.context,
                    'related_files': node.related_files, 'dependency_results': dependencies}, ensure_ascii=False))
                task_input = {'workspace': str(checkout.path) if checkout else state['workspace'],
                              'task': task, 'messages': state.get('messages', messages or [])}
                result = await execution_graph.ainvoke(task_input, config=config)
                entry = dict(result['execution_result'])
                produced = message_delta(result.get('messages', []), state.get('messages', messages or []))
                response_error = result.get('response_error', '')
                if checkout:
                    entry['integration'] = checkout.info()
                if entry.get('status') == 'completed':
                    if checkout:
                        await git_operation(checkout.commit_execution)
                        entry['integration'] = checkout.info()
                    # Execute success is provisional until the owning group is reviewed.
                    entry['status'] = 'executed' if reviewer else 'completed'
            except Exception as exc:
                entry.update(status='failed', error=f'Task execution/integration failed: {exc}')
                if checkout:
                    entry['integration'] = checkout.info()
            return {'results': {node.id: entry}, 'messages': produced, 'response_error': response_error}
        return execute

    def review_node(group_id, checkpoint):
        async def review(state, config: RunnableConfig):
            entries = state['results']
            members = [key for key in checkpoint['members'] if entries[key]['status'] == 'executed']
            if not members:
                return {}
            produced, response_error = [], ''
            candidate, merged, warnings = None, None, []
            assessment = {'status': 'failed', 'output': '', 'error': '', 'assessment': {'status': 'fail'}}
            try:
                external = set().union(*(ancestors[key] for key in members)) - set(members)
                if any(entries[key]['status'] != 'completed' for key in external):
                    raise ValueError('A prerequisite review failed; this group cannot be integrated')
                task = review_task(group_id, members, nodes, entries)
                aggregate = {'status': 'completed',
                    'output': entries[members[0]].get('output', '') if len(members) == 1 else json.dumps(
                        {key: entries[key].get('output', '') for key in members}, ensure_ascii=False),
                    'task_results': {key: entries[key] for key in members}}

                async def invoke_reviewer(workspace):
                    nonlocal assessment, produced, response_error
                    result = await reviewer.ainvoke({'workspace': workspace, 'task': task,
                        'execution_result': aggregate,
                        'messages': preprocess_messages(state.get('messages', messages or []))}, config=config)
                    assessment = {key: result.get(key, '') for key in FIELDS}
                    produced = message_delta(result.get('messages', []), state.get('messages', messages or []))
                    response_error = result.get('response_error', '')

                if isolated:
                    candidate = await git_operation(TaskCheckout, state['workspace'], group_id.lstrip('_'))
                    await candidate.create([entries[key]['integration']['commit'] for key in members])
                    await git_operation(candidate.commit_execution)
                    async with candidate.locked():
                        await git_operation(candidate.prepare_review)
                        aggregate['integration'] = {**candidate.info(), 'base_commit': candidate.merge_base,
                            'commit': candidate.review_commit, 'review_group': group_id, 'task_ids': members}
                        await invoke_reviewer(str(candidate.review_path))
                        if review_passed(assessment):
                            merged = await git_operation(candidate.integrate)
                            try:
                                warnings += await git_operation(candidate.cleanup_success)
                                for key in members:
                                    warnings += await git_operation(cleanup_execution_worktree,
                                        state['workspace'], entries[key]['integration']['worktree'])
                            except Exception:
                                warnings.append('Merge completed; generated worktrees could not be removed')
                else:
                    await invoke_reviewer(state['workspace'])
            except Exception as exc:
                assessment.update(status='failed', error=f'Group review/integration failed: {exc}')
                details = assessment.get('assessment')
                assessment['assessment'] = {**(details if isinstance(details, dict) else {}),
                    'status': 'fail', 'explanation': assessment['error'], 'blockers': [assessment['error']]}
            passed = review_passed(assessment)
            assessment.update(review_group=group_id, task_ids=members)
            updates = {}
            for key in members:
                entry = {**entries[key], 'review': assessment, 'review_group': group_id,
                    'status': 'completed' if passed else 'failed',
                    'error': '' if passed else (assessment.get('error') or 'Reviewer rejected task group')}
                if candidate:
                    entry['integration'] = {**entry.get('integration', {}), 'group_branch': candidate.branch,
                        'group_worktree': str(candidate.path), 'review_worktree': str(candidate.review_path)}
                    if merged:
                        entry['integration'].update(status='merged', merged_commit=merged['merged_commit'])
                        if warnings:
                            entry['integration']['cleanup_warnings'] = warnings
                updates[key] = entry
            label = ', '.join(members)
            progress_id = members[0] if len(members) == 1 else group_id.lstrip('_')
            content = (f'Reviewer đã duyệt nhóm {label}' + (' và merge Git local.' if merged else '.')
                       if passed else f'Nhóm {label} chưa được duyệt hoặc tích hợp: {assessment.get("error", "")}')
            if passed and isinstance(assessment.get('assessment'), dict):
                content += ' ' + assessment['assessment'].get('explanation', '')
            produced.append(progress_message('reviewer', progress_id, content.strip()))
            return {'results': updates, 'messages': produced, 'response_error': response_error}
        return review

    def completion(key):
        return after.get(key, key) if reviewer else key

    jobs = {}
    for node in plan.nodes:
        prerequisites = [before[node.id]] if reviewer and node.id in before else [completion(p) for p in parents[node.id]]
        jobs[node.id] = (prerequisites, logged_node('network', node.id, execute_node(node),
                                                   task_id=node.id, rewrite_responses=False))
    if reviewer:
        for group_id, checkpoint in checkpoints.items():
            triggers = [key if after.get(key) == group_id else completion(key) for key in checkpoint['triggers']]
            prerequisites = list(dict.fromkeys([*triggers, *checkpoint['requires']]))
            jobs[group_id] = (prerequisites, logged_node('network', group_id, review_node(group_id, checkpoint),
                                                        rewrite_responses=False))

    async def run(state, config: RunnableConfig, runtime: Runtime):
        # LangGraph supersteps wait for unrelated slow branches. Schedule these
        # dependency jobs individually so only actual prerequisites block a join.
        current = {**state, 'results': dict(state.get('results', {})),
                   'messages': add_messages([], state.get('messages', messages or []))}
        initial_messages = list(current['messages'])
        done = {key: asyncio.Event() for key in jobs}
        writer = runtime.stream_writer
        limit = config.get('max_concurrency')
        semaphore = asyncio.Semaphore(limit) if limit is not None else None

        async def perform(key):
            dependencies, action = jobs[key]
            for dependency in dependencies:
                await done[dependency].wait()
            async def invoke():
                snapshot = {**current, 'results': dict(current['results']), 'messages': list(current['messages'])}
                return await action.ainvoke(snapshot, config=get_config() if sys.version_info >= (3, 11) else config)
            if semaphore is None:
                return await invoke()
            async with semaphore:
                return await invoke()

        async def apply(key, future):
            # Cached task results must also be applied in dependency order.
            for dependency in jobs[key][0]:
                await done[dependency].wait()
            update = await future
            current['results'].update(update.get('results', {}))
            current['messages'] = add_messages(current['messages'], update.get('messages', []))
            if update.get('response_error'):
                current['response_error'] = update['response_error']
            writer({'node': key, 'update': update})
            done[key].set()

        # Register durable calls in a stable topological order, including review
        # jobs. Replays reuse completed outputs instead of repeating Git/CLI work.
        ordered = []
        remaining = dict(jobs)
        while remaining:
            for key in list(remaining):
                if all(dependency in ordered for dependency in remaining[key][0]):
                    ordered.append(key)
                    del remaining[key]
        futures = {}
        for key in ordered:
            if sys.version_info >= (3, 11):
                futures[key] = durable_task(name=key)(perform)(key)
            else:
                # LangGraph's async functional tasks require Python 3.11+.
                futures[key] = asyncio.create_task(perform(key), name=key)
        tasks = [asyncio.create_task(apply(key, future), name=key) for key, future in futures.items()]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for future in [*futures.values(), *tasks]:
                future.cancel()
            await asyncio.gather(*futures.values(), *tasks, return_exceptions=True)
            raise
        return {'results': current['results'],
                'messages': message_delta(current['messages'], initial_messages),
                'response_error': current.get('response_error', '')}

    graph.add_node('run_review_network', run)
    graph.add_edge(START, 'run_review_network')
    graph.add_edge('run_review_network', END)
    return graph.compile()
