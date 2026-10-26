"""Readable runtime milestones without logging prompt bodies or credentials."""
import asyncio
import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import importlib
import logging
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4
import sys
from time import perf_counter
from urllib.parse import urlsplit

from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda

_CONTEXT = ContextVar('orchestrator_log_context', default={})
LOGGER = logging.getLogger('orchestrator')
_CONFIG_LOCK = RLock()
_LISTENER = None
_QUEUE_HANDLER = None
_LOG_TARGET = None


def shutdown_logging():
    global _LISTENER, _QUEUE_HANDLER
    with _CONFIG_LOCK:
        if _QUEUE_HANDLER is not None:
            LOGGER.removeHandler(_QUEUE_HANDLER)
            _QUEUE_HANDLER = None
        if _LISTENER is not None:
            listener, _LISTENER = _LISTENER, None
            listener.stop()
            for handler in listener.handlers:
                handler.close()


def configure_logging():
    """Configure handlers in a worker thread when called by async nodes."""
    global _LISTENER, _QUEUE_HANDLER, _LOG_TARGET
    level = os.getenv('ORCHESTRATOR_LOG_LEVEL', 'INFO').upper()
    if level not in {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}:
        raise ValueError('Invalid ORCHESTRATOR_LOG_LEVEL')
    with _CONFIG_LOCK:
        configured = os.getenv('ORCHESTRATOR_LOG_FILE')
        if not configured and os.getenv('CLI_LOG_DIR'):
            configured = str(Path(os.environ['CLI_LOG_DIR']) / 'orchestrator.log')
        target = str(Path(configured).expanduser().resolve()) if configured else None
        if _LISTENER is None or target != _LOG_TARGET:
            shutdown_logging()
            handlers = [logging.StreamHandler(sys.stderr)]
            if target:
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(mode=0o600, exist_ok=True)
                handlers.append(logging.FileHandler(path, encoding='utf-8'))
            formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')
            for handler in handlers:
                handler.setFormatter(formatter)
            queue = SimpleQueue()
            _QUEUE_HANDLER = QueueHandler(queue)
            _LISTENER = QueueListener(queue, *handlers)
            _LOG_TARGET = target
            _LISTENER.start()
        if _QUEUE_HANDLER not in LOGGER.handlers:
            LOGGER.addHandler(_QUEUE_HANDLER)
        LOGGER.setLevel(level)
        LOGGER.propagate = False


def event(name, *, level=logging.INFO, **fields):
    if not LOGGER.isEnabledFor(level):
        return
    secrets = {value for key, value in os.environ.items() if value and
               any(word in key.upper() for word in ('TOKEN', 'API_KEY', 'PASSWORD', 'SECRET'))}
    def safe(value):
        text = str(value)
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        return json.dumps(text, ensure_ascii=False)
    values = {**_CONTEXT.get(), **fields}
    LOGGER.log(level, 'event=%s %s', name,
               ' '.join(f'{key}={safe(value)}' for key, value in values.items() if value is not None))


@contextmanager
def operation(name, **fields):
    started = perf_counter()
    result = {}
    event(name + '.start', **fields)
    try:
        yield result
    except BaseException as exc:
        cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
        event(name + ('.cancelled' if cancelled else '.error'),
              level=logging.WARNING if cancelled else logging.ERROR,
              **fields, elapsed_ms=round((perf_counter() - started) * 1000, 1),
              error_type=type(exc).__name__)
        raise
    else:
        event(name + '.end', **fields, **result,
              elapsed_ms=round((perf_counter() - started) * 1000, 1))


def outcome(update):
    """Summarize state updates; never dump messages, reports, or error strings."""
    if not isinstance(update, dict):
        return {}
    result = {key: update[key] for key in ('status', 'replan_count') if key in update}
    for key in ('execution_result', 'review'):
        if isinstance(update.get(key), dict):
            result[key + '_status'] = update[key].get('status')
    if isinstance(update.get('assessment'), dict):
        result['assessment'] = update['assessment'].get('status')
    if isinstance(update.get('plan'), dict):
        result['plan_nodes'] = len(update['plan'].get('nodes', []))
    if isinstance(update.get('results'), dict):
        statuses = [item.get('status') for item in update['results'].values()]
        result.update({f'tasks_{status}': statuses.count(status)
                       for status in ('completed', 'failed', 'skipped')})
    if update.get('response_error'):
        result['has_response_error'] = True
    if update.get('error') or update.get('cli_error'):
        result['has_error'] = True
    return result


def logged_node(agent, name, action, *, task_id=None, response_options=None, rewrite_responses=True):
    """Wrap a node preserving sync/async invocation, config, and callbacks."""
    runnable = action if isinstance(action, Runnable) else RunnableLambda(action)

    @contextmanager
    def scope(state, config):
        metadata = config.get('metadata', {})
        configurable = config.get('configurable', {})
        token = _CONTEXT.set({**_CONTEXT.get(), 'agent': agent, 'node': name,
            'task_id': state.get('task', {}).get('task_id') or task_id or _CONTEXT.get().get('task_id'),
            'thread_id': configurable.get('thread_id') or metadata.get('thread_id'),
            'attempt': state.get('replan_count', 0) + 1 if 'replan_count' in state else _CONTEXT.get().get('attempt'),
            'step': metadata.get('langgraph_step'),
            'node_run_id': uuid4().hex})
        try:
            with operation('node') as summary:
                yield summary
        finally:
            _CONTEXT.reset(token)

    def prepare_response_module():
        configure_logging()
        return importlib.import_module('orchestrator.runtime.responses')

    def invoke(state, config: RunnableConfig):
        responses = prepare_response_module()
        with responses.response_scope(response_options), scope(state, config) as summary:
            update = runnable.invoke(state, config=config)
            if rewrite_responses:
                update = responses.rewrite_update(state, update, config, agent=agent, node=name)
            summary.update(outcome(update))
            return update

    async def ainvoke(state, config: RunnableConfig):
        responses = await asyncio.to_thread(prepare_response_module)
        with responses.response_scope(response_options), scope(state, config) as summary:
            update = await runnable.ainvoke(state, config=config)
            if rewrite_responses:
                update = await responses.arewrite_update(state, update, config, agent=agent, node=name)
            summary.update(outcome(update))
            return update

    return RunnableLambda(invoke, afunc=ainvoke, name=f'{agent}.{name}')


def endpoint_origin(value):
    try:
        parsed = urlsplit(value or '')
        host = parsed.hostname or ''
        host = f'[{host}]' if ':' in host else host
        return f'{parsed.scheme}://{host}' + (f':{parsed.port}' if parsed.port else '')
    except ValueError:
        return 'invalid-endpoint'


def llm_fields(options, *, purpose, schema=None):
    return {'purpose': purpose, 'schema': schema,
        'model': options.get('structure_model') or os.getenv('DECISION_MODEL') or options.get('model') or os.getenv('QWEN_MODEL'),
        'endpoint': endpoint_origin(options.get('structure_base_url') or os.getenv('DECISION_BASE_URL') or options.get('base_url') or os.getenv('QWEN_BASE_URL'))}


def log_cli(function):
    @wraps(function)
    def invoke(prompt, cli='qwen', **kwargs):
        configure_logging()
        fields = {'cli': cli, 'runtime': os.getenv('CODING_CLI_RUNTIME', 'local'),
                  'model': kwargs.get('model') or os.getenv('QWEN_MODEL'),
                  'endpoint': endpoint_origin(kwargs.get('base_url') or os.getenv('QWEN_BASE_URL')),
                  'timeout_s': kwargs.get('timeout', 1800), 'prompt_chars': len(prompt)}
        with operation('cli', **fields) as summary:
            result = function(prompt, cli, **kwargs)
            summary['output_chars'] = len(result)
            return result
    return invoke


atexit.register(shutdown_logging)
