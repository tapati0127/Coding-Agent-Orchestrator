"""Isolated task branches and serialized, review-gated local Git integration."""
import asyncio
from contextlib import asynccontextmanager, ExitStack
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4
from .logging import event, operation


def isolation_enabled():
    value = os.getenv('GIT_TASK_ISOLATION', 'true').strip().lower()
    if value not in {'true', 'false'}:
        raise ValueError('GIT_TASK_ISOLATION must be true or false')
    return value == 'true'


def git(path, *args, check=True):
    result = subprocess.run(['git', '-c', 'user.name=Orchestrator',
        '-c', 'user.email=orchestrator@localhost', '-C', str(path), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(f'git {args[0]} failed: {(result.stderr or result.stdout).strip()}')
    return result


async def run_blocking(function, *args):
    """Finish an in-flight operation before letting cancellation release its resources."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def git_operation(function, *args):
    """Keep the integration lock until an in-flight Git operation has finished."""
    with operation('git.operation', action=function.__name__):
        return await run_blocking(function, *args)


class TaskCheckout:
    def __init__(self, workspace, task_id):
        self.workspace = Path(workspace).resolve(strict=True)
        top = Path(git(self.workspace, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
        if top != self.workspace:
            raise ValueError('Git task workspace must be the repository root')
        self.target = os.getenv('GIT_TARGET_BRANCH') or 'main'
        git(self.workspace, 'check-ref-format', '--branch', self.target)
        self.common = Path(git(self.workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
        self.token = uuid4().hex
        self.branch = f'orchestrator/task-{task_id}-{self.token}'
        self.review_branch = f'orchestrator/review-{task_id}-{self.token}'
        parent = Path(os.getenv('GIT_WORKTREE_ROOT') or Path(tempfile.gettempdir()) / 'orchestrator-worktrees').expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / f'task-{self.token}'
        self.review_path = parent / f'review-{self.token}'
        self.base = ''
        self.merge_base = ''
        self.review_commit = ''
        self.task_commit = ''

    @asynccontextmanager
    async def locked(self):
        # flock coordinates independent workflow instances/processes as well as tasks.
        event('git.lock.wait', branch=self.branch, target=self.target)
        resources = ExitStack()
        try:
            lock = await run_blocking(lambda: resources.enter_context(
                (self.common / 'orchestrator-integration.lock').open('a')))
            while True:
                if await run_blocking(try_lock, lock):
                    break
                await asyncio.sleep(.05)
            event('git.lock.acquired', branch=self.branch, target=self.target)
            try:
                yield
            finally:
                await run_blocking(fcntl.flock, lock, fcntl.LOCK_UN)
                event('git.lock.released', branch=self.branch)
        finally:
            await run_blocking(resources.close)

    def check_main(self):
        branch = git(self.workspace, 'symbolic-ref', '--short', 'HEAD').stdout.strip()
        if branch != self.target:
            raise ValueError(f'Workspace must have target branch {self.target!r} checked out (found {branch!r})')
        if git(self.workspace, 'status', '--porcelain').stdout.strip():
            raise ValueError('Target workspace must be clean; commit or stash existing changes before execution')
        return git(self.workspace, 'rev-parse', 'HEAD').stdout.strip()

    async def create(self, dependency_commits=()):
        async with self.locked():
            self.base = await git_operation(self.check_main)
            await git_operation(git, self.workspace, 'worktree', 'add', '-b', self.branch, str(self.path), self.base)
        for commit in dict.fromkeys(dependency_commits):
            await git_operation(git, self.path, 'merge', '--no-ff', '--no-edit', commit)
        return self

    def commit_execution(self):
        if git(self.path, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != self.branch:
            raise ValueError('Executor changed its assigned branch')
        git(self.path, 'add', '--all')
        changed = git(self.path, 'diff', '--cached', '--quiet', check=False)
        if changed.returncode not in (0, 1):
            raise RuntimeError('Cannot inspect executor changes')
        if changed.returncode == 1:
            git(self.path, 'commit', '-m', f'Execute {self.branch}')
        self.task_commit = git(self.path, 'rev-parse', 'HEAD').stdout.strip()

    def prepare_review(self):
        self.merge_base = self.check_main()
        git(self.workspace, 'worktree', 'add', '-b', self.review_branch, str(self.review_path), self.merge_base)
        # Conflicts remain in the isolated review tree, never in the main checkout.
        git(self.review_path, 'merge', '--no-ff', '--no-edit', self.task_commit)
        self.review_commit = git(self.review_path, 'rev-parse', 'HEAD').stdout.strip()
        if git(self.review_path, 'status', '--porcelain').stdout.strip():
            raise ValueError('Integration candidate is not clean')

    def integrate(self):
        if self.check_main() != self.merge_base:
            raise ValueError('Target branch changed during review; a fresh review is required')
        if (git(self.review_path, 'rev-parse', 'HEAD').stdout.strip() != self.review_commit
                or git(self.review_path, 'status', '--porcelain').stdout.strip()):
            raise ValueError('Reviewer modified the integration candidate; merge refused')
        git(self.workspace, 'merge', '--ff-only', self.review_commit)
        event('git.merged', branch=self.branch, target=self.target, commit=self.review_commit)
        return {'branch': self.branch, 'target_branch': self.target,
                'commit': self.task_commit, 'merged_commit': self.review_commit,
                'status': 'merged'}

    def info(self):
        return {'branch': self.branch, 'target_branch': self.target,
                'worktree': str(self.path), 'review_worktree': str(self.review_path),
                'base_commit': self.base, 'commit': self.task_commit, 'status': 'not_merged'}

    def cleanup_success(self):
        # No --force: do not discard unexpected edits, even in generated checkouts.
        warnings = []
        for path in (self.path, self.review_path):
            result = git(self.workspace, 'worktree', 'remove', str(path), check=False)
            if result.returncode:
                warnings.append(f'Worktree retained: {path}')
        return warnings


def try_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def merged_commit_is_ancestor(workspace, commit, target):
    return git(workspace, 'merge-base', '--is-ancestor', commit, target, check=False).returncode == 0


def cleanup_execution_worktree(workspace, path):
    result = git(workspace, 'worktree', 'remove', path, check=False)
    return [] if result.returncode == 0 else [f'Worktree retained: {path}']
