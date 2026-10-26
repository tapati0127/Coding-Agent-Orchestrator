"""An extra ChatOpenAI pass for human-facing prose, independent of business decisions."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
from uuid import uuid4
import yaml

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from .artifacts import (ARTIFACT_KEY, ARTIFACTS_KEY, RESPONSE_KEY, attach_artifacts,
                        is_artifact_only, preprocess_messages)
from .chat import build_chat_model, no_stream_config
from .logging import event, operation, llm_fields
from .thinking import thinking_options

_OPTIONS = ContextVar('response_options', default={})
CONFIG_PATH = Path(__file__).resolve().parents[2] / 'config' / 'response.yaml'


@contextmanager
def response_scope(options=None):
    token = _OPTIONS.set(dict(options) if options is not None else _OPTIONS.get())
    try:
        yield
    finally:
        _OPTIONS.reset(token)


def response_settings():
    settings = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8'))
    if not isinstance(settings, dict):
        raise ValueError('response config must be a mapping')
    for key in ('timeout', 'history_messages', 'source_chars'):
        value = settings.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'response.{key} must be a positive integer')
    if not isinstance(settings.get('prompt'), str) or not settings['prompt'].strip():
        raise ValueError('response.prompt must not be empty')
    return settings


def build_response_model(options):
    settings = response_settings()
    effective = thinking_options(settings, options)
    # This is plain-text generation, with intermediate token streaming suppressed.
    model = build_chat_model(effective, settings['timeout'], structured=True)
    return model, settings


def _redact(text, options):
    secrets = {v for k, v in os.environ.items() if v and
               any(word in k.upper() for word in ('TOKEN', 'API_KEY', 'PASSWORD', 'SECRET'))}
    secrets.update(v for k, v in options.items() if k.endswith('api_key') and isinstance(v, str) and v)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    return text


def response_inputs(state, update, message, settings, options, *, agent, node):
    # Do this before slicing history: artifact payloads never consume the history budget.
    history = preprocess_messages(state.get('messages', []))[-settings['history_messages']:]
    safe_history = [{'role': m.type, 'name': getattr(m, 'name', None), 'content': str(m.content)} for m in history]
    effective = {**state, **update}
    facts = {key: effective.get(key) for key in
             ('status', 'error', 'cli_error', 'assessment', 'clarification') if effective.get(key)}
    report = effective.get('output') or effective.get('natural_plan') or ''
    facts['report'] = str(report)[:settings['source_chars']]
    if effective.get('task'):
        facts['task'] = effective['task'].get('instructions', '')
    if effective.get('plan'):
        facts['planned_tasks'] = len(effective['plan'].get('nodes', []))
    if effective.get('results'):
        facts['results'] = {key: {field: value.get(field) for field in
            ('status', 'error', 'assessment', 'integration') if value.get(field)}
            for key, value in effective['results'].items()}
    draft = preprocess_messages([message])[0].content
    payload = {'agent': agent, 'node': node, 'history': safe_history,
               'draft': str(draft)[:settings['source_chars']], 'facts': facts,
               'source_truncated': len(str(draft)) > settings['source_chars'] or len(str(report)) > settings['source_chars']}
    source = _redact(json.dumps(payload, ensure_ascii=False, default=str), options)
    return [SystemMessage(content=settings['prompt']), HumanMessage(content=source)]


def _rewritten(original, reply, options):
    if not isinstance(reply, AIMessage) or not isinstance(reply.content, str) or not reply.content.strip():
        raise ValueError('Response model must return nonempty plain-text AIMessage')
    clean = preprocess_messages([original])[0]
    metadata = dict(clean.additional_kwargs)
    metadata[RESPONSE_KEY] = {'rewritten': True}
    message = original.model_copy(update={'id': original.id or str(uuid4()),
        'content': _redact(reply.content.strip(), options), 'additional_kwargs': metadata,
        'response_metadata': reply.response_metadata, 'usage_metadata': reply.usage_metadata})
    existing = original.additional_kwargs.get(ARTIFACTS_KEY, [])
    artifacts = [AIMessage(content=item['content'], additional_kwargs={ARTIFACT_KEY:
        {k:v for k,v in item.items() if k != 'content'}}) for item in existing]
    return attach_artifacts(message, artifacts)


def _already_rewritten(message):
    marker = message.additional_kwargs.get(RESPONSE_KEY)
    return isinstance(marker, dict) and marker.get('rewritten') is True


def _assemble(update, rewritten):
    """Only attach artifacts after LLM calls; do not send their data back to a model."""
    outgoing = []
    artifacts = []
    for message in update['messages']:
        if is_artifact_only(message):
            artifacts.append(message)
        elif not isinstance(message, AIMessage):
            outgoing.append(message)
        elif _already_rewritten(message):
            outgoing.append(message)
        elif id(message) in rewritten:
            outgoing.append(rewritten[id(message)])
        elif message.additional_kwargs.get(ARTIFACTS_KEY):
            artifacts.extend(AIMessage(content=item['content'], additional_kwargs={ARTIFACT_KEY:
                {k:v for k,v in item.items() if k != 'content'}})
                for item in message.additional_kwargs[ARTIFACTS_KEY])
    if artifacts:
        target = next((i for i in range(len(outgoing)-1, -1, -1) if isinstance(outgoing[i], AIMessage)), None)
        if target is None:
            outgoing.extend(artifacts)
        else:
            outgoing[target] = attach_artifacts(outgoing[target], artifacts)
    return {**update, 'messages': outgoing}


def rewrite_update(state, update, config, *, agent, node):
    if not isinstance(update, dict) or not update.get('messages'):
        return update
    options = _OPTIONS.get()
    rewritten = {}
    for message in update['messages']:
        if not isinstance(message, AIMessage) or is_artifact_only(message) or _already_rewritten(message):
            continue
        try:
            with operation('llm', **llm_fields(options, purpose='user_response')) as summary:
                model, settings = build_response_model(options)
                reply = model.invoke(response_inputs(state, update, message, settings, options, agent=agent, node=node),
                                     config=no_stream_config(config))
                rewritten[id(message)] = _rewritten(message, reply, options)
                summary['output_chars'] = len(reply.content)
                summary['token_usage'] = reply.usage_metadata
        except Exception as exc:
            # Presentation must not roll back, retry, or hide an already performed Git merge.
            event('response.omitted', error_type=type(exc).__name__)
    result = _assemble(update, rewritten)
    if len(rewritten) < sum(isinstance(m, AIMessage) and not is_artifact_only(m) and not _already_rewritten(m)
                            for m in update['messages']):
        result['response_error'] = 'User-facing response generation failed; see runtime logs'
    return result


async def arewrite_update(state, update, config, *, agent, node):
    if not isinstance(update, dict) or not update.get('messages'):
        return update
    options = _OPTIONS.get()
    rewritten = {}
    for message in update['messages']:
        if not isinstance(message, AIMessage) or is_artifact_only(message) or _already_rewritten(message):
            continue
        try:
            with operation('llm', **llm_fields(options, purpose='user_response')) as summary:
                model, settings = await asyncio.to_thread(build_response_model, options)
                inputs = response_inputs(state, update, message, settings, options, agent=agent, node=node)
                reply = await asyncio.wait_for(model.ainvoke(inputs, config=no_stream_config(config)),
                                               timeout=settings['timeout'])
                rewritten[id(message)] = _rewritten(message, reply, options)
                summary['output_chars'] = len(reply.content)
                summary['token_usage'] = reply.usage_metadata
        except Exception as exc:
            event('response.omitted', error_type=type(exc).__name__)
    result = _assemble(update, rewritten)
    if len(rewritten) < sum(isinstance(m, AIMessage) and not is_artifact_only(m) and not _already_rewritten(m)
                            for m in update['messages']):
        result['response_error'] = 'User-facing response generation failed; see runtime logs'
    return result
