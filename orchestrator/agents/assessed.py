"""Shared task/report adapters for Execute and Reviewer."""
from string import Template

from orchestrator.schemas import Assessment
from orchestrator.state import TaskState
from orchestrator.runtime.two_phase import build_two_phase_graph, read_settings, workspace_path


def build_assessed_agent(config_path, *, role, enrich_task=None, cli_runner=None,
                         chat_factory=None, **llm_options):
    settings = read_settings(config_path)

    def prepare(state):
        task = state.get('task')
        if not isinstance(task, dict):
            raise ValueError('task must be a mapping')
        if not isinstance(task.get('instructions'), str) or not task['instructions'].strip():
            raise ValueError('task.instructions must be a nonempty string')
        workspace = workspace_path(state.get('workspace'))
        task = enrich_task(state) if enrich_task else dict(task)
        values = {'workspace': workspace, 'instructions': task['instructions']}
        for key in ('task_id', 'context', 'acceptance_criteria', 'constraints'):
            value = task.get(key, '')
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                value = '\n'.join(f'- {item}' for item in value)
            if not isinstance(value, str):
                raise ValueError(f'task.{key} must be a string or list of strings')
            values[key] = value or '(Không cung cấp)'
        return {'workspace': workspace, 'task': task,
                'prompt': Template(settings['prompt']).substitute(values),
                'status': 'ready', 'output': '', 'cli_error': '', 'error': '', 'assessment': {}}

    def payload(state):
        return {'task': state['task'], 'workspace': state['workspace'],
                'report': state['output'], 'cli_error': state['cli_error']}

    def finish(state, assessment):
        if not assessment.explanation.strip():
            raise ValueError('Empty explanation')
        if state['cli_error'] or assessment.blockers:
            assessment = assessment.model_copy(update={'status': 'fail'})
        passed = assessment.status == 'pass'
        return {'assessment': assessment.model_dump(),
                'status': 'completed' if passed else 'failed',
                'error': '' if passed else (state['cli_error'] or assessment.explanation)}

    def failure(state, reason):
        reason = f'{role} assessment: {reason}'
        return {'status': 'failed', 'error': state.get('cli_error') or reason,
                'assessment': Assessment(status='fail', notes=[reason], explanation=reason,
                    blockers=[state['cli_error']] if state.get('cli_error') else []).model_dump()}

    return build_two_phase_graph(state_schema=TaskState, output_schema=Assessment,
        settings=settings, prepare=prepare, payload=payload, finish=finish, failure=failure,
        llm_options=llm_options, cli_runner=cli_runner, chat_factory=chat_factory, agent_name=role.lower(),
        rewrite_responses=False, compact_output=True)
