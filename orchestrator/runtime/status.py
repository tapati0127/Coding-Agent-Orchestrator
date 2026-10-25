"""Milestone messages: one replaceable progress message per role/task."""
from langchain_core.messages import AIMessage


TEAM_PROGRESS_ID = 'team-progress'


def progress_message(role, task_id, content):
    return AIMessage(id=f'progress:{role}:{task_id or "main"}', name='progress', content=content)


def team_progress(content):
    return AIMessage(id=TEAM_PROGRESS_ID, name='team_progress', content=content)
