"""Human-readable plan artifacts, excluded from model/agent inputs."""
from html import escape
from uuid import uuid4
from langchain_core.messages import AIMessage
from orchestrator.schemas import PlanGraph, validate_plan_graph

ARTIFACT_KEY = 'orchestrator_artifact'
ARTIFACT_TYPE = 'execution_plan'


ARTIFACTS_KEY = 'orchestrator_artifacts'
TEXT_KEY = 'orchestrator_text'
RESPONSE_KEY = 'orchestrator_response'


def is_artifact_only(message):
    return (isinstance(message, AIMessage)
            and isinstance(message.additional_kwargs.get(ARTIFACT_KEY), dict))


def attach_artifacts(message, artifacts):
    """Append deterministic markdown after generation, retaining an exact clean text copy."""
    if not artifacts:
        return message
    metadata = dict(message.additional_kwargs)
    plain = metadata.get(TEXT_KEY, message.content)
    existing = list(metadata.get(ARTIFACTS_KEY, []))
    for artifact in artifacts:
        existing.append({**artifact.additional_kwargs[ARTIFACT_KEY], 'content': artifact.content})
    metadata[TEXT_KEY] = plain
    metadata[ARTIFACTS_KEY] = existing
    content = str(plain) + '\n\n' + '\n\n'.join(str(item['content']) for item in existing)
    return message.model_copy(update={'content': content, 'additional_kwargs': metadata})


def preprocess_messages(messages):
    """Remove every owned artifact type; preserve the prose of mixed messages."""
    clean = []
    for message in messages:
        if is_artifact_only(message):
            continue
        if isinstance(message, AIMessage):
            metadata = dict(message.additional_kwargs)
            content = message.content
            if ARTIFACTS_KEY in metadata:
                # Fail closed if a foreign/malformed mixed message has no clean prose.
                content = metadata.pop(TEXT_KEY, '')
                metadata.pop(ARTIFACTS_KEY, None)
            metadata.pop(RESPONSE_KEY, None)
            if not content:
                continue
            message = message.model_copy(update={'content': content, 'additional_kwargs': metadata})
        clean.append(message)
    return clean


def plan_artifact(plan: PlanGraph, *, attempt=1):
    """Render the validated execution DAG deterministically, without an LLM call."""
    parents = validate_plan_graph(plan)
    ids = {node.id: f'task_{i}' for i, node in enumerate(plan.nodes)}
    lines = [f'### Kế hoạch thực thi · lần {attempt}', '',
             'Các nhánh độc lập có thể chạy song song. Nhiệm vụ chỉ bắt đầu khi tất cả phụ thuộc được duyệt.',
             'Mỗi nhiệm vụ chạy Execute → Reviewer; khi bật Git isolation, nhiệm vụ chỉ hoàn tất sau merge local. Nhánh phụ thuộc dừng nếu nhiệm vụ trước không đạt.',
             '', '```mermaid', 'flowchart TD', '    start([Bắt đầu])', '    finish([Kết thúc])']
    for node in plan.nodes:
        title = ' '.join(node.instructions.split())
        title = title[:97] + '…' if len(title) > 100 else title
        label = escape(f'{node.id}: {title}', quote=True).replace('`', '&#96;')
        lines.append(f'    {ids[node.id]}["{label}"]')
    for node in plan.nodes:
        if not parents[node.id]:
            lines.append(f'    start --> {ids[node.id]}')
    for edge in plan.edges:
        lines.append(f'    {ids[edge.source]} --> {ids[edge.target]}')
    sources = {edge.source for edge in plan.edges}
    for node in plan.nodes:
        if node.id not in sources:
            lines.append(f'    {ids[node.id]} --> finish')
    lines += ['```', '', '| Nhiệm vụ | Nội dung | Chờ nhiệm vụ | Tiêu chí hoàn thành |',
              '| --- | --- | --- | --- |']
    def cell(value):
        return escape(' '.join(value.split()), quote=False).replace('|', '&#124;').replace('`', '&#96;')
    for node in plan.nodes:
        lines.append('| ' + ' | '.join(map(cell, [node.id, node.instructions,
            ', '.join(parents[node.id]) or 'Không có', '; '.join(node.acceptance_criteria)])) + ' |')
    return AIMessage(content='\n'.join(lines), name='plan', additional_kwargs={
        ARTIFACT_KEY: {'type': ARTIFACT_TYPE, 'mime_type': 'text/markdown',
                       'version': 1, 'read_only': True, 'attempt': attempt}})


def plan_handoff_artifact(*, attempt=1, approved_count=0):
    """Display-only milestone emitted before invoking the planning subgraph."""
    content = (f'### Bàn giao cho AI Plan · lần {attempt}\n\n'
               '| Bước | Tiến độ |\n| --- | --- |\n'
               '| Tiếp nhận yêu cầu | Đã hoàn tất |\n'
               '| Bàn giao cho AI Plan | Đang chuyển |\n'
               '| Khảo sát code và lập kế hoạch | Tiếp theo |\n'
               '| Tạo sơ đồ nhiệm vụ và phụ thuộc | Sau khi kế hoạch được kiểm tra |\n\n'
               'Plan sẽ khảo sát repository và tạo kế hoạch trước khi triển khai. '
               'Xem log CLI tại [Terminal Monitor](/terminals).')
    if attempt > 1:
        content += f'\n\nLập lại kế hoạch; giữ {approved_count} nhiệm vụ đã được Reviewer duyệt.'
    return AIMessage(id=f'plan-handoff:{attempt}:{uuid4().hex}', name='plan_handoff',
        content=content, additional_kwargs={ARTIFACT_KEY: {
            'type': 'plan_handoff', 'mime_type': 'text/markdown', 'version': 1,
            'read_only': True, 'attempt': attempt}})
