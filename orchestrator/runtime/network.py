"""Build dependency-aware Execute/Reviewer task networks from a validated DAG."""
import json
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from .artifacts import preprocess_messages
from .logging import logged_node
from orchestrator.schemas import PlanGraph

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
    graph.add_node('execute', logged_node('task', 'execute', execute, rewrite_responses=False))
    graph.add_edge(START, 'execute')
    if reviewer is not None:
        graph.add_node('reviewer', logged_node('task', 'reviewer', review, rewrite_responses=False))
        graph.add_conditional_edges('execute', lambda s: 'reviewer'
            if s['execution_result']['status'] == 'completed' else END, ['reviewer', END])
        graph.add_edge('reviewer', END)
    else:
        graph.add_edge('execute', END)
    return graph.compile()


def build_execution_graph(plan: PlanGraph, executor, *, reviewer=None, messages=None, approved_work=None):
    """Review at dependency joins, explicit checkpoints and the end of the plan."""
    from .review_network import build_review_network
    return build_review_network(plan, executor, reviewer=reviewer,
                                messages=messages, approved_work=approved_work)
