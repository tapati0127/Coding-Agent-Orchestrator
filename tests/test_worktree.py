import subprocess
from pathlib import Path

import pytest

from orchestrator.runtime.worktree import WorkspaceError, WorkspaceManager


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    repository = root / "repositories" / "demo"
    repository.mkdir(parents=True)
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@localhost")
    (repository / "README.md").write_text("initial\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "initial")
    manager = WorkspaceManager(root, tmp_path / "data")
    manager.register("demo", repository)
    return manager, repository


def test_create_project_has_matching_directory_and_committed_baseline_and_preserves_retry(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace", tmp_path / "data")
    project = manager.create("new-project")
    repository = Path(project["repository_path"])
    assert repository == manager.root / "new-project"
    assert git(repository, "rev-parse", "HEAD") == project["base_commit"]
    (repository / "keep.txt").write_text("preserve this")
    assert manager.create("new-project") == project
    assert (repository / "keep.txt").read_text() == "preserve this"
    assert manager.provision("new-project", "execute-01", task_id="task")["base_commit"] == project["base_commit"]


def test_create_refuses_existing_files_or_symlinked_directory(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace", tmp_path / "data")
    repository = manager.root / "existing"
    repository.mkdir(parents=True)
    (repository / "keep.txt").write_text("preserve")
    with pytest.raises(WorkspaceError, match="not empty"):
        manager.create("existing")
    assert not (repository / ".git").exists()
    (manager.root / "linked").symlink_to(repository)
    with pytest.raises(WorkspaceError, match="Symlinked"):
        manager.create("linked")


def test_isolated_worktrees_capture_and_integrate(workspace):
    manager, repository = workspace
    first = manager.provision("demo", "execute-01", task_id="first")
    second = manager.provision("demo", "execute-02", task_id="second")
    (Path(first["worktree_path"]) / "a.txt").write_text("one")
    assert not (Path(second["worktree_path"]) / "a.txt").exists()
    (Path(second["worktree_path"]) / "b.txt").write_text("two")
    a = manager.capture("demo", "execute-01", "first")
    b = manager.capture("demo", "execute-02", "second")
    assert Path(a["patch_path"]).read_text()
    assert not (repository / "a.txt").exists()
    integrated = manager.integrate("demo", [a["commit_sha"], b["commit_sha"]])
    assert integrated["status"] == "SUCCESS"
    assert (Path(integrated["worktree_path"]) / "a.txt").read_text() == "one"
    assert (Path(integrated["worktree_path"]) / "b.txt").read_text() == "two"
    assert manager.integrate("demo", [a["commit_sha"], b["commit_sha"]])["base_commit"] == integrated["base_commit"]
    after = manager.provision("demo", "execute-01", task_id="next")
    assert after["base_commit"] == integrated["base_commit"]
    assert after["branch"] != a["branch"]
    assert git(repository, "rev-parse", a["branch"]) == a["commit_sha"]


def test_restart_ownership_and_preserves_dirty_work(workspace):
    manager, _ = workspace
    worker = manager.provision("demo", "execute-01", task_id="first")
    edited = Path(worker["worktree_path"]) / "uncommitted.txt"
    edited.write_text("preserve me")
    resumed = WorkspaceManager(manager.root, manager.data_root)
    assert resumed.provision("demo", "execute-01", task_id="first") == worker
    with pytest.raises(WorkspaceError, match="another task"):
        resumed.provision("demo", "execute-01", task_id="other")
    assert edited.read_text() == "preserve me"
    result = resumed.capture("demo", "execute-01", "first")
    patch = Path(result["patch_path"]).read_text()
    resumed.capture("demo", "execute-01", "first")
    assert Path(result["patch_path"]).read_text() == patch
    assert resumed.provision("demo", "execute-01", task_id="first")["branch"] == worker["branch"]
    resumed.capture("demo", "execute-01", "first")
    edited.write_text("new edits")
    with pytest.raises(WorkspaceError, match="Dirty"):
        resumed.provision("demo", "execute-01", task_id="other")
    assert edited.read_text() == "new edits"


def test_verify_can_preserve_fixes_across_multiple_tasks(workspace):
    manager, _ = workspace
    integration = manager.integrate("demo", [])
    path = Path(integration["worktree_path"])
    (path / "fix.txt").write_text("one")
    first = manager.capture("demo", "integration", "verify-first")
    (path / "fix.txt").write_text("two")
    second = manager.capture("demo", "integration", "verify-second")
    assert first["commit_sha"] != second["commit_sha"]


def test_integration_conflicts_remain_observable(workspace):
    manager, _ = workspace
    first = manager.provision("demo", "execute-01", task_id="first")
    second = manager.provision("demo", "execute-02", task_id="second")
    (Path(first["worktree_path"]) / "README.md").write_text("first\n")
    (Path(second["worktree_path"]) / "README.md").write_text("second\n")
    a = manager.capture("demo", "execute-01", "first")
    b = manager.capture("demo", "execute-02", "second")
    result = manager.integrate("demo", [a["commit_sha"], b["commit_sha"]])
    assert result["status"] == "CONFLICT"
    assert result["conflicts"] == ["README.md"]
    assert "<<<<<<<" in (Path(result["worktree_path"]) / "README.md").read_text()
    assert manager.integrate("demo", [b["commit_sha"]])["status"] == "CONFLICT"


def test_capture_and_integrate_all_qwen_commits_plus_uncommitted_tail(workspace):
    manager, repository = workspace
    worker = manager.provision("demo", "execute-01", task_id="multi")
    path = Path(worker["worktree_path"])
    (path / "first.txt").write_text("first commit\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "Qwen first change")
    first_commit = git(path, "rev-parse", "HEAD")
    (path / "second.txt").write_text("second commit\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "Qwen second change")
    second_commit = git(path, "rev-parse", "HEAD")
    (path / "third.txt").write_text("uncommitted tail\n")
    captured = manager.capture("demo", "execute-01", "multi")
    patch = Path(captured["patch_path"]).read_text()
    assert all(name in patch for name in ("first.txt", "second.txt", "third.txt"))
    result = manager.integrate("demo", [captured["commit_sha"]])
    assert result["status"] == "SUCCESS"
    assert {first_commit, second_commit, captured["commit_sha"]}.issubset(result["integrated_commits"])
    integration_path = Path(result["worktree_path"])
    assert all((integration_path / name).exists() for name in ("first.txt", "second.txt", "third.txt"))
    assert manager.verify_integrated(repository, captured["commit_sha"], result["base_commit"])
    before = git(integration_path, "rev-list", "--count", "HEAD")
    assert manager.integrate("demo", [captured["commit_sha"]])["status"] == "SUCCESS"
    assert git(integration_path, "rev-list", "--count", "HEAD") == before
    # A baseline containing only the final tail commit cannot claim integration
    # of the earlier Qwen commits in the same task.
    lone = manager.provision("demo", "execute-02", base_commit=worker["base_commit"], task_id="lone")
    git(lone["worktree_path"], "cherry-pick", captured["commit_sha"])
    assert not manager.verify_integrated(repository, captured["commit_sha"], git(lone["worktree_path"], "rev-parse", "HEAD"))


def test_conflict_capture_retains_provenance_and_does_not_repeat_pick(workspace):
    manager, repository = workspace
    first = manager.provision("demo", "execute-01", task_id="first")
    second = manager.provision("demo", "execute-02", task_id="second")
    (Path(first["worktree_path"]) / "README.md").write_text("first\n")
    (Path(second["worktree_path"]) / "README.md").write_text("second\n")
    a = manager.capture("demo", "execute-01", "first")
    b = manager.capture("demo", "execute-02", "second")
    result = manager.integrate("demo", [a["commit_sha"], b["commit_sha"]])
    assert result["status"] == "CONFLICT"
    path = Path(result["worktree_path"])
    (path / "README.md").write_text("combined resolution\n")
    git(path, "add", "README.md")
    resolved = manager.capture("demo", "integration", "verify-resolve")
    assert f"(cherry picked from commit {b['commit_sha']})" in git(path, "log", "-1", "--format=%B")
    assert subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "CHERRY_PICK_HEAD"], capture_output=True).returncode != 0
    before = git(path, "rev-list", "--count", "HEAD")
    retried = WorkspaceManager(manager.root, manager.data_root).integrate("demo", [a["commit_sha"], b["commit_sha"]])
    assert retried["status"] == "SUCCESS"
    assert retried["base_commit"] == resolved["commit_sha"]
    assert git(path, "rev-list", "--count", "HEAD") == before
    assert not manager.verify_integrated(repository, b["commit_sha"], retried["base_commit"])


@pytest.mark.parametrize("value", ["../outside", "a/../../b", "a..b", "/absolute", "-bad"])
def test_rejects_path_identifiers(workspace, value):
    manager, repository = workspace
    with pytest.raises(WorkspaceError):
        manager.register(value, repository)


def test_rejects_symlink_escape(workspace, tmp_path):
    manager, _ = workspace
    (manager.root / "worktrees").symlink_to(tmp_path / "outside")
    with pytest.raises(WorkspaceError, match="escapes|Symlinked"):
        manager.provision("demo", "execute-01")
