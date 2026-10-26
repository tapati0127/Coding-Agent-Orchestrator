"""Thinking policy shared by ChatOpenAI, Qwen CLI and OpenCode CLI."""
import os

FORMATS = {'enable_thinking', 'chat_template_kwargs'}


def thinking_options(settings=None, options=None):
    settings, options = settings or {}, dict(options or {})
    enabled = settings.get('thinking')
    if enabled is None:
        enabled = options.get('thinking')
    if enabled is None:
        raw = os.getenv('LLM_THINKING', 'false').strip().lower()
        if raw not in {'true', 'false', '1', '0', 'yes', 'no'}:
            raise ValueError('LLM_THINKING must be true or false')
        enabled = raw in {'true', '1', 'yes'}
    if not isinstance(enabled, bool):
        raise ValueError('thinking must be a boolean or null')
    fmt = settings.get('thinking_format') or options.get('thinking_format') or os.getenv('LLM_THINKING_FORMAT', 'enable_thinking')
    if fmt not in FORMATS:
        raise ValueError('thinking_format must be enable_thinking or chat_template_kwargs')
    return {**options, 'thinking': enabled, 'thinking_format': fmt}


def thinking_body(*, thinking=None, thinking_format=None):
    options = thinking_options(options={'thinking': thinking, 'thinking_format': thinking_format})
    enabled = options['thinking']
    if options['thinking_format'] == 'chat_template_kwargs':
        return {'chat_template_kwargs': {'enable_thinking': enabled}}
    return {'enable_thinking': enabled}
