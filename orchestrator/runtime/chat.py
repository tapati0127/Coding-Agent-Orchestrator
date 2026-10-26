"""Shared ChatOpenAI configuration for natural and structured calls."""
import os
from langchain_openai import ChatOpenAI
from langchain_core.runnables.config import merge_configs
from .thinking import thinking_body


def no_stream_config(config=None):
    return merge_configs(config, {'tags': ['no_stream']})


def build_chat_model(options, timeout, *, chat_factory=None, structured=False):
    endpoint = options.get('structure_base_url') or os.getenv('DECISION_BASE_URL') or options.get('base_url') or os.getenv('QWEN_BASE_URL')
    model = options.get('structure_model') or os.getenv('DECISION_MODEL') or options.get('model') or os.getenv('QWEN_MODEL')
    key = options.get('structure_api_key')
    if key is None:
        key = os.getenv('DECISION_API_KEY') or options.get('api_key') or os.getenv('QWEN_API_KEY') or 'local'
    if not endpoint or not model:
        raise ValueError('Chat LLM endpoint and model are required')
    extra_body = thinking_body(thinking=options.get('thinking'),
                               thinking_format=options.get('thinking_format'))
    return (chat_factory or ChatOpenAI)(base_url=endpoint, model=model, api_key=key,
        timeout=timeout, max_retries=0, streaming=not structured, disable_streaming=structured, extra_body=extra_body)
