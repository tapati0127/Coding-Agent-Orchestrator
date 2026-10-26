"""Build dependency-aware Execute/Reviewer task networks from a validated DAG."""
import asyncio
import json
import os
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from .artifacts import preprocess_messages
from .git_tasks import TaskCheckout, git_operation, isolation_enabled, merged_commit_is_ancestor
from .status import progress_message
from .logging import logged_node, event
from orchestrator.schemas import ExecuteNode, PlanGraph, validate_plan_graph

def message_delta(current, previous):
    previous_by_id = {message.id: message for message in previous if getattr(message, 'id', None)}
    clean_by_id = {message.id: message for message in preprocess_messages(previous)
                   if getattr(message, 'id', None)}
    # Agent inputs intentionally have display artifacts/markers stripped. That
    # stripped history must not replace the original mixed messages in parent state.
    return [message for message in current
            if (message != previous_by_id.get(getattr(message, 'id', None))
                and message != clean_by_id.get(getattr(message, 'id', None)))]


def merge_results(left: dict, right: dict) -> dict:
    return {**left, **right}


def merge_response_errors(left: str, right: str) -> str:
    return right or left


class ExecutionState(TypedDict):
    workspace: str
    response_error: Annotated[str, merge_response_errors]
    results: Annotated[dict[str, dict], merge_results]
    messages: Annotated[list[AnyMessage], add_messages]


def task_signature(task: dict) -> str:
    # Ignore generated IDs/context so a renamed, otherwise identical task is reused.
    return json.dumps({key: task.get(key) for key in (
        "instructions", "related_files", "acceptance_criteria", "constraints")}, sort_keys=True, ensure_ascii=False)


class TaskRunState(TypedDict, total=False):
    workspace: str
    response_error: str
    task: dict
    messages: Annotated[list[AnyMessage], add_messages]
    execution_result: dict
    review: dict


def build_task_graph(executor, reviewer=None):
    """Compose Execute and Reviewer role graphs for a single planned node."""
    fields = ('status', 'output', 'error', 'assessment')

    async def execute(state: TaskRunState, config: RunnableConfig):
        produced = []
        result = {}
        try:
            result = await executor.ainvoke({'workspace': state['workspace'], 'task': state['task'],
                'messages': preprocess_messages(state.get('messages', []))}, config=config)
            entry = {key: result.get(key, '') for key in fields}
            produced = message_delta(result.get('messages', []), state.get('messages', []))
        except Exception:
            entry = {'status': 'failed', 'output': '', 'error': 'Execute graph failed'}
        update = {'execution_result': entry, 'messages': produced}
        if result.get('response_error'):
            update['response_error'] = result['response_error']
        return update

    async def review(state: TaskRunState, config: RunnableConfig):
        produced = []
        result = {}
        try:
            result = await reviewer.ainvoke({'workspace': state['workspace'], 'task': state['task'],
                'messages': preprocess_messages(state.get('messages', [])), 'execution_result': state['execution_result']}, config=config)
            entry = {key: result.get(key, '') for key in fields}
            produced = message_delta(result.get('messages', []), state.get('messages', []))
        except Exception:
            entry = {'status': 'failed', 'output': '', 'error': 'Reviewer graph failed'}
        update = {'review': entry, 'messages': produced}
        if result.get('response_error'):
            update['response_error'] = result['response_error']
        return update

    graph = StateGraph(TaskRunState)
    graph.add_node('execute', logged_node('task', 'execute', execute))
    graph.add_edge(START, 'execute')
    if reviewer is not None:
        graph.add_node('reviewer', logged_node('task', 'reviewer', review))
        graph.add_conditional_edges('execute', lambda s: 'reviewer'
            if s['execution_result']['status'] == 'completed' else END, ['reviewer', END])
        graph.add_edge('reviewer', END)
    else:
        graph.add_edge('execute', END)
    return graph.compile()


async def run_isolated_task(executor, reviewer, state, config):
    """Execute in a private branch; review the latest merged candidate under lock."""
    checkout = None
    produced = []
    entry = {'status': 'failed', 'output': '', 'error': ''}
    review = None
    response_error = ''
    try:
        if reviewer is None:
            raise ValueError('Isolated execution requires a Reviewer before merging')
        checkout = await git_operation(TaskCheckout, state['workspace'], state['task']['task_id'])
        await checkout.create()
        execution_graph = await asyncio.to_thread(build_task_graph, executor)
        execution = await execution_graph.ainvoke(
            {**state, 'workspace': str(checkout.path)}, config=config)
        produced = message_delta(execution.get('messages', []), state.get('messages', []))
        response_error = execution.get('response_error', '')
        entry = dict(execution['execution_result'])
        entry['integration'] = checkout.info()
        if entry.get('status') != 'completed':
            return {'execution_result': entry, 'messages': produced, 'response_error': response_error}
        await git_operation(checkout.commit_execution)
        entry['integration'] = checkout.info()
        async with checkout.locked():
            await git_operation(checkout.prepare_review)
            # Reviewer sees the candidate against the current main, not an old task base.
            result = await reviewer.ainvoke({'workspace': str(checkout.review_path),
                'task': state['task'], 'execution_result': entry,
                'messages': preprocess_messages([*state.get('messages', []), *produced])}, config=config)
            response_error = result.get('response_error') or response_error
            review = {key: result.get(key, '') for key in ('status', 'output', 'error', 'assessment')}
            produced += message_delta(result.get('messages', []), [*state.get('messages', []), *produced])
            if review['status'] == 'completed' and review.get('assessment', {}).get('status') == 'pass':
                entry['integration'] = await git_operation(checkout.integrate)
                try:
                    warnings = await git_operation(checkout.cleanup_success)
                except Exception:
                    warnings = ['Merge completed; generated worktrees could not be removed']
                if warnings:
                    entry['integration']['cleanup_warnings'] = warnings
                produced.append(progress_message('reviewer', state['task']['task_id'],
                    f"Reviewer đã duyệt và merge {checkout.branch} vào {checkout.target}."))
    except Exception as exc:
        entry['status'] = 'failed'
        entry['error'] = f'Git task integration failed: {exc}'
        if checkout is not None:
            entry['integration'] = checkout.info()
        produced.append(progress_message('reviewer', state['task']['task_id'],
            'Chưa merge nhiệm vụ vào nhánh chính; cần Plan xử lý lỗi tích hợp.'))
    update = {'execution_result': entry, 'messages': produced}
    if response_error:
        update['response_error'] = response_error
    if review is not None:
        update['review'] = review
    return update


def build_execution_graph(plan: PlanGraph, executor, *, reviewer=None, messages=None, approved_work=None):
    """Build actual fan-out/join edges, with one Execute invocation per node."""
    parents = validate_plan_graph(plan)
    isolated = isolation_enabled()
    graph = StateGraph(ExecutionState)
    task_graph = build_task_graph(executor, reviewer)
    approved = {task_signature(item["task"]): item["result"] for item in (approved_work or [])}

    def make_node(node: ExecuteNode):
        async def run(state: ExecutionState, config: RunnableConfig):
            prior = approved.get(task_signature(node.model_dump()))
            if prior is not None and isolated:
                integration = prior.get('integration', {})
                commit = integration.get('merged_commit')
                if (integration.get('status') != 'merged' or not commit
                        or not await git_operation(merged_commit_is_ancestor, state['workspace'], commit,
                                                  os.getenv('GIT_TARGET_BRANCH') or 'main')):
                    prior = None
            if prior is not None:
                event('task.reused', task_id=node.id)
                return {"results": {node.id: {**prior, "reused": True}}}
            produced = []
            response_error = ''
            dependencies = {key: state["results"][key] for key in parents[node.id]}
            if any(item["status"] != "completed" for item in dependencies.values()):
                event('task.skipped', task_id=node.id, reason='dependency_failed')
                entry = {"status": "skipped", "output": "", "error": "Dependency failed"}
            else:
                task = node.model_dump(exclude={"id"})
                task["task_id"] = node.id
                task["context"] = json.dumps({"findings": node.context,
                    "related_files": node.related_files, "dependency_results": dependencies}, ensure_ascii=False)
                task_input = {'workspace': state['workspace'], 'task': task,
                    'messages': state.get('messages', messages or [])}
                if isolated:
                    result = await run_isolated_task(executor, reviewer, task_input, config)
                else:
                    result = await task_graph.ainvoke(task_input, config=config)
                produced = message_delta(result.get('messages', []), state.get('messages', messages or []))
                response_error = result.get('response_error', '')
                entry = dict(result['execution_result'])
                if 'review' in result:
                    entry['review'] = result['review']
                    if (result['review'].get('status') != 'completed' or
                            result['review'].get('assessment', {}).get('status') != 'pass'):
                        entry['status'] = 'failed'
                        entry['error'] = result['review'].get('error') or 'Reviewer rejected Execute result'
            update = {"results": {node.id: entry}, "messages": produced}
            if response_error:
                update['response_error'] = response_error
            return update
        return run

    for node in plan.nodes:
        graph.add_node(node.id, logged_node('network', node.id, make_node(node), task_id=node.id))
        # A list creates an ALL-predecessors barrier, including unequal branches.
        graph.add_edge(parents[node.id] or START, node.id)
    sources = {edge.source for edge in plan.edges}
    for node in plan.nodes:
        if node.id not in sources:
            graph.add_edge(node.id, END)
    return graph.compile()
