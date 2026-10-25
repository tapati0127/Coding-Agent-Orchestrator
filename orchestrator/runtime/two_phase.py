"""One implementation of prepare -> coding CLI -> structured LLM for all roles."""
import asyncio
import json
import os
from pathlib import Path
import subprocess

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
import yaml

from .cli import run_llm_cli
from .chat import build_chat_model, no_stream_config
from .status import progress_message

CONFIG_ROOT = Path(__file__).resolve().parents[2] / 'config'


def read_settings(path):
    settings = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(settings, dict):
        raise ValueError('Agent YAML must contain a mapping')
    if settings.get('cli', 'qwen') not in {'qwen', 'opencode'}:
        raise ValueError('cli must be qwen or opencode')
    for key, default in (('timeout', 1800), ('structure_timeout', 120)):
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f'{key} must be positive')
    for key in ('prompt', 'structure_prompt'):
        if not isinstance(settings.get(key), str) or not settings[key].strip():
            raise ValueError(f'{key} must be a nonempty string')
    if settings.get('structured_output_method', 'json_schema') not in {'json_schema', 'function_calling', 'json_mode'}:
        raise ValueError('Invalid structured_output_method')
    return settings


def workspace_path(value=None):
    if value is None:
        value = os.getenv('WORKSPACE_ROOT') or str(Path.cwd())
    if not isinstance(value, str) or not value.strip():
        raise ValueError('workspace is required')
    workspace = Path(value).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('workspace must be a directory')
    return str(workspace)


def build_two_phase_graph(*, state_schema, output_schema, settings, prepare,
                          payload, finish, failure, llm_options=None,
                          cli_runner=None, chat_factory=None, stop_on_cli_error=False,
                          agent_name='agent', natural_node=None):
    """Role hooks adapt input/output; CLI/config/model/error lifecycle is shared.

    State schema must expose workspace, prompt, output and cli_error. Hooks:
    prepare(state)->updates; payload(state)->JSON data; finish(state, model)->updates;
    failure(state, reason)->updates. CLI failures can still be assessed, but the
    finish hook must never upgrade a failed CLI to a successful task.
    """
    options = llm_options or {}
    cli_runner = cli_runner or run_llm_cli
    chat_factory = chat_factory or ChatOpenAI
    timeout = settings.get('timeout', 1800)
    structure_timeout = settings.get('structure_timeout', 120)

    def coding(state):
        try:
            output = cli_runner(state['prompt'], cli=settings.get('cli', 'qwen'),
                cwd=state['workspace'], timeout=timeout,
                **{key: options.get(key) for key in ('base_url', 'model', 'api_key')})
            if not output.strip():
                raise ValueError('CLI returned empty output')
            return {'output': output, 'cli_error': '',
                    'messages': [AIMessage(content=output, name=agent_name)]}
        except subprocess.TimeoutExpired:
            reason = f'CLI timed out after {timeout}s'
        except Exception as exc:
            reason = str(exc) or 'CLI failed'
        # Do not expose a credential if an external CLI echoes it in its errors.
        for key in (options.get('api_key'), os.getenv('QWEN_API_KEY')):
            if key:
                reason = reason.replace(key, '[REDACTED]')
        update = {'output': '', 'cli_error': reason}
        if stop_on_cli_error:
            update.update(failure({**state, **update}, reason))
        return update

    def structured_model():
        return build_chat_model(options, structure_timeout, chat_factory=chat_factory,
            structured=True).with_structured_output(
                output_schema, method=settings.get('structured_output_method', 'json_schema'))

    def inputs(state):
        return [SystemMessage(content=settings['structure_prompt']),
                HumanMessage(content=json.dumps(payload(state), ensure_ascii=False))]

    def structure(state, config: RunnableConfig):
        try:
            output = structured_model().invoke(inputs(state), config=no_stream_config(config))
            return finish(state, output_schema.model_validate(output))
        except Exception:
            return failure(state, 'Structured output failed or returned an invalid result')

    async def astructure(state, config: RunnableConfig):
        try:
            output = await asyncio.wait_for(structured_model().ainvoke(inputs(state), config=no_stream_config(config)), timeout=structure_timeout)
            return finish(state, output_schema.model_validate(output))
        except Exception:
            return failure(state, 'Structured output failed or returned an invalid result')

    def announce_start(state):
        task_id = state.get('task', {}).get('task_id', '')
        label = {'plan': 'Plan đang khảo sát codebase và lập kế hoạch',
                 'execute': 'Execute bắt đầu triển khai nhiệm vụ',
                 'reviewer': 'Reviewer bắt đầu kiểm tra code và test'}.get(agent_name, 'Bắt đầu xử lý')
        suffix = f' {task_id}' if task_id else ''
        return {'messages': [progress_message(agent_name, task_id, label + suffix + '.')]}

    def announce_result(state):
        task_id = state.get('task', {}).get('task_id', '')
        label = agent_name.capitalize() + (f' {task_id}' if task_id else '')
        if state.get('status') == 'planned':
            content = f'Plan đã lập kế hoạch gồm {len(state["plan"]["nodes"])} nhiệm vụ.'
        elif state.get('status') == 'completed':
            content = f'{label}: pass. ' + state.get('assessment', {}).get('explanation', '')
        else:
            content = f'{label}: chưa hoàn thành. ' + state.get('error', 'Không có kết quả hợp lệ.')
        return {'messages': [progress_message(agent_name, task_id, content)]}

    graph = StateGraph(state_schema)
    graph.add_node('prepare', prepare)
    graph.add_node('coding', natural_node if natural_node is not None else coding)
    graph.add_node('structure', RunnableLambda(structure, afunc=astructure).with_config(tags=['no_stream']))
    graph.add_edge(START, 'prepare')
    if natural_node is None:
        graph.add_node('announce_start', announce_start)
        graph.add_node('announce_result', announce_result)
        graph.add_edge('prepare', 'announce_start')
        graph.add_edge('announce_start', 'coding')
    else:
        graph.add_edge('prepare', 'coding')
    if stop_on_cli_error:
        failed_destination = 'announce_result' if natural_node is None else END
        graph.add_conditional_edges('coding', lambda s: failed_destination if s.get('cli_error') else 'structure',
                                    ['structure', failed_destination])
    else:
        graph.add_edge('coding', 'structure')
    if natural_node is None:
        graph.add_edge('structure', 'announce_result')
        graph.add_edge('announce_result', END)
    else:
        graph.add_edge('structure', END)
    return graph.compile()
