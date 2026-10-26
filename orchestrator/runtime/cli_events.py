"""Render native CLI events live, and extract the final report for the agent."""
import json


def _record(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(block.get('text', '') for block in content
                       if isinstance(block, dict) and block.get('type') == 'text')
    return ''


def report_text(stdout, cli):
    """Keep protocol records/tool output out of the structured assessment input."""
    records = [record for line in stdout.splitlines() if (record := _record(line))]
    if cli == 'qwen':
        results = [r for r in records if r.get('type') == 'result']
        if results:
            result = results[-1]
            if result.get('is_error'):
                raise RuntimeError(_text(result.get('error', {}).get('message')) or 'Qwen failed')
            return _text(result.get('result')).strip()
        messages = [_text(r.get('message', {}).get('content')) for r in records
                    if r.get('type') == 'assistant' and not r.get('parent_tool_use_id')]
    else:
        errors = [r for r in records if r.get('type') == 'error']
        if errors:
            raise RuntimeError(json.dumps(errors[-1].get('error'), ensure_ascii=False))
        messages = [r.get('part', {}).get('text', '') for r in records if r.get('type') == 'text']
    if messages:
        return '\n\n'.join(messages).strip()
    # Retain compatibility with text-only wrappers; event-only output is empty.
    return '' if any(r.get('type') in {'system', 'stream_event', 'step_start', 'step_finish', 'tool_use'}
                     for r in records) else stdout.strip()


class EventRenderer:
    """JSONL framing happens before redaction, so escaped/split secrets stay hidden."""
    def __init__(self, cli):
        self.cli = cli
        self.pending = ''
        self.partial = set()

    def feed(self, text, *, final=False):
        self.pending += text
        lines = self.pending.split('\n')
        self.pending = '' if final else lines.pop()
        return ''.join(self.render(line) for line in lines if line)

    def render(self, line):
        record = _record(line)
        if not record or not isinstance(record.get('type'), str):
            return line + '\n'
        kind = record['type']
        if self.cli == 'opencode':
            part = record.get('part', {})
            if kind == 'text':
                return part.get('text', '') + '\n'
            if kind == 'step_start':
                return '\n[LLM] Đang xử lý…\n'
            if kind == 'tool_use':
                state = part.get('state', {})
                return f"\n[Tool · {part.get('tool', '')}] {state.get('title', '')}\n" + str(
                    state.get('output') or state.get('error') or '') + '\n'
            if kind == 'error':
                return '\n[Lỗi] ' + json.dumps(record.get('error'), ensure_ascii=False) + '\n'
            return ''
        parent = record.get('parent_tool_use_id')
        if kind == 'stream_event':
            event = record.get('event', {})
            subtype = event.get('type')
            if subtype == 'message_start':
                self.partial.discard(parent)
                return '\n[LLM] Đang xử lý…\n'
            if subtype == 'content_block_delta':
                delta = event.get('delta', {})
                if delta.get('type') == 'text_delta':
                    self.partial.add(parent)
                    return delta.get('text', '')
            if subtype == 'content_block_start':
                block = event.get('content_block', {})
                if block.get('type') == 'tool_use':
                    return f"\n[Tool] {block.get('name', '')}\n"
            if subtype == 'tool_progress':
                return '\n[Tiến độ tool] ' + json.dumps(event.get('content'), ensure_ascii=False) + '\n'
            return ''
        if kind == 'assistant':
            content = record.get('message', {}).get('content', [])
            text = '' if parent in self.partial else _text(content)
            tools = ''.join(f"\n[Tool] {b.get('name', '')} {json.dumps(b.get('input'), ensure_ascii=False)}\n"
                            for b in content if isinstance(b, dict) and b.get('type') == 'tool_use')
            return text + '\n' + tools
        if kind == 'user':
            content = record.get('message', {}).get('content', [])
            return ''.join('\n[Kết quả tool] ' + _text(b.get('content')) + '\n'
                           for b in content if isinstance(b, dict) and b.get('type') == 'tool_result')
        if kind == 'result' and record.get('is_error'):
            return '\n[Lỗi] ' + json.dumps(record.get('error'), ensure_ascii=False) + '\n'
        return ''
