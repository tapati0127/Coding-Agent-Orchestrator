"""Per-invocation scratch directories; project changes live outside cleanup."""
from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def coding_workdir(workspace, *, cli):
    project = Path(workspace or Path.cwd()).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError('workspace must be a directory')
    parent = os.getenv('AGENT_WORKDIR_ROOT') or None
    if parent:
        parent = str(Path(parent).expanduser().resolve())
        Path(parent).mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f'coding-{cli}-', dir=parent) as temporary:
        workdir = Path(temporary)
        (workdir / 'project').symlink_to(project, target_is_directory=True)
        (workdir / 'tmp').mkdir()
        yield workdir, project
