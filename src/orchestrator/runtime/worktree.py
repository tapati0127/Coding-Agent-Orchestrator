"""Persistent Git worktrees, with no destructive recovery or implicit pushing.

Worktrees isolate working directories, not hostile processes. The dispatcher owns
worker assignment; optional task IDs also protect against accidental worker reuse.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, workspace_root: str | Path = "/workspace", data_root: str | Path = "/data"):
        self.root = Path(workspace_root).resolve()
        self.data_root = Path(data_root).resolve()

    @staticmethod
    def _identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) or ".." in value:
            raise WorkspaceError("Invalid project, worker or task identifier")
        return value

    def _safe(self, *parts: str) -> Path:
        candidate = self.root.joinpath(*parts)
        if not candidate.resolve().is_relative_to(self.root):
            raise WorkspaceError("Workspace path escapes workspace root")
        # Do not follow symlinks, even when they happen to point within the root.
        for ancestor in (candidate, *candidate.parents):
            if ancestor == self.root:
                break
            if ancestor.is_symlink():
                raise WorkspaceError("Symlinked workspace paths are not supported")
        return candidate

    @staticmethod
    def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if check and result.returncode:
            # Git output can contain repository remotes, so do not surface it to
            # public events. Local conflict paths are exposed separately below.
            raise WorkspaceError(f"Git {args[0] if args else 'operation'} failed (exit {result.returncode})")
        return result

    @contextlib.contextmanager
    def _locked(self, project_id: str):
        project_id = self._identifier(project_id)
        directory = self.data_root / "workspaces"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{project_id}.json"
        if path.is_symlink() or (directory / f"{project_id}.lock").is_symlink():
            raise WorkspaceError("Symlinked metadata is not supported")
        with (directory / f"{project_id}.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            metadata = json.loads(path.read_text()) if path.exists() else None
            yield metadata, path

    @staticmethod
    def _save(path: Path, metadata: dict):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(metadata, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _registered(self, metadata: dict | None) -> Path:
        if not metadata:
            raise WorkspaceError("Project repository is not registered")
        repository = Path(metadata["repository_path"])
        # Recheck persisted paths on every operation; configuration can change.
        if not repository.resolve().is_relative_to(self.root):
            raise WorkspaceError("Registered repository escapes workspace root")
        return self._safe(str(repository.relative_to(self.root)))

    def register(self, project_id: str, local_path: str | Path) -> dict:
        """Register an existing, committed repository without moving or editing it."""
        project_id = self._identifier(project_id)
        original = Path(local_path).absolute()
        if not original.resolve().is_relative_to(self.root):
            raise WorkspaceError("Repository must be within workspace root")
        repository = self._safe(str(original.relative_to(self.root)))
        root = Path(self._git(repository, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if root != repository.resolve():
            raise WorkspaceError("Register the repository root, not a subdirectory")
        head = self._git(repository, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        with self._locked(project_id) as (metadata, path):
            if metadata and metadata["repository_path"] != str(repository):
                raise WorkspaceError("Project is already registered to another repository")
            metadata = metadata or {"project_id": project_id, "repository_path": str(repository), "initial_commit": head, "workers": {}, "integrated_commits": [], "captures": {}}
            self._save(path, metadata)
            return {"project_id": project_id, "repository_path": str(repository), "base_commit": head}

    @staticmethod
    def _clean(path: Path) -> bool:
        return not WorkspaceManager._git(path, "status", "--porcelain").stdout.strip()

    def _provision(self, metadata: dict, project_id: str, worker_id: str, base_commit: str | None, task_id: str | None) -> dict:
        repository = self._registered(metadata)
        if worker_id != "integration" and not re.fullmatch(r"execute-0[1-5]", worker_id):
            raise WorkspaceError("Worker must be execute-01..05 or integration")
        if task_id is not None:
            self._identifier(task_id)
        workers = metadata["workers"]
        integration = workers.get("integration")
        baseline = base_commit or (self._git(Path(integration["worktree_path"]), "rev-parse", "HEAD").stdout.strip() if integration else metadata["initial_commit"])
        # --end-of-options prevents user-controlled revs from becoming switches.
        baseline = self._git(repository, "rev-parse", "--verify", "--end-of-options", f"{baseline}^{{commit}}").stdout.strip()
        worktree = self._safe("worktrees", project_id, worker_id)
        info = workers.get(worker_id)
        if info:
            if str(worktree) != info["worktree_path"] or not worktree.exists():
                raise WorkspaceError("Registered worktree is missing or relocated; preserve and reconcile manually")
            branch = self._git(worktree, "symbolic-ref", "--short", "HEAD").stdout.strip()
            if branch != info["branch"]:
                raise WorkspaceError("Worktree branch changed outside the workspace manager")
            if worker_id == "integration":
                return dict(info)
            if info.get("active", False):
                if task_id and info.get("task_id") != task_id:
                    raise WorkspaceError("Worktree is already assigned to another task")
                return dict(info)
            if task_id and info.get("task_id") == task_id:
                # A technical retry resumes the task branch and its preserved
                # commits, even when the previous attempt captured artifacts.
                info["active"] = True
                return dict(info)
            if not self._clean(worktree):
                raise WorkspaceError("Dirty worktree preserved; capture or reconcile it before reuse")
        elif worktree.exists():
            raise WorkspaceError("Unregistered worktree path already exists; refusing to overwrite")
        if worker_id == "integration":
            branch = f"orchestrator/{project_id}/integration"
        else:
            branch = f"orchestrator/{project_id}/{worker_id}/{task_id or uuid.uuid4().hex}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if info:
            self._git(worktree, "switch", "-c", branch, baseline)
        else:
            # Recover a branch created before a crash; never reset it.
            exists = self._git(repository, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
            args = ("worktree", "add", str(worktree), branch) if exists else ("worktree", "add", "-b", branch, str(worktree), baseline)
            self._git(repository, *args)
        info = {"project_id": project_id, "worker_id": worker_id, "worktree_path": str(worktree), "branch": branch, "base_commit": baseline, "task_id": task_id, "active": worker_id != "integration"}
        workers[worker_id] = info
        return dict(info)

    def provision(self, project_id: str, worker_id: str, base_commit: str | None = None, task_id: str | None = None) -> dict:
        project_id, worker_id = self._identifier(project_id), self._identifier(worker_id)
        with self._locked(project_id) as (metadata, path):
            self._registered(metadata)
            info = self._provision(metadata, project_id, worker_id, base_commit, task_id)
            self._save(path, metadata)
            return info

    def capture(self, project_id: str, worker_id: str, task_id: str) -> dict:
        """Preserve tracked/untracked changes as a binary patch and local commit."""
        for identifier in (project_id, worker_id, task_id):
            self._identifier(identifier)
        with self._locked(project_id) as (metadata, path):
            self._registered(metadata)
            info = metadata["workers"].get(worker_id)
            if not info:
                raise WorkspaceError("Worker has no worktree")
            if worker_id != "integration" and info.get("task_id") not in (None, task_id):
                raise WorkspaceError("Task does not own this worktree")
            worktree = self._safe("worktrees", project_id, worker_id)
            if self._git(worktree, "symbolic-ref", "--short", "HEAD").stdout.strip() != info["branch"]:
                raise WorkspaceError("Worktree branch changed outside the workspace manager")
            if self._git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.strip():
                raise WorkspaceError("Unresolved conflicts preserved; capture cannot commit them")
            self._git(worktree, "add", "--all")
            # Save the entire task delta, including commits Qwen itself made,
            # rather than only the unstaged tail of the task.
            patch = self._git(worktree, "diff", "--cached", "--binary", info["base_commit"]).stdout
            artifact_dir = self._safe("artifacts", project_id, "patches")
            artifact_dir.mkdir(parents=True, exist_ok=True)
            patch_path = artifact_dir / f"{task_id}.patch"
            if patch_path.is_symlink():
                raise WorkspaceError("Symlinked artifact path is not supported")
            # A repeated capture must not replace the preserved patch with an
            # empty patch once the prior invocation has committed its changes.
            if patch or not patch_path.exists():
                patch_path.write_text(patch)
            staged = self._git(worktree, "diff", "--cached", "--quiet", check=False).returncode != 0
            pending = self._git(worktree, "rev-parse", "--verify", "CHERRY_PICK_HEAD", check=False)
            if worker_id == "integration" and pending.returncode == 0:
                source_commit = pending.stdout.strip()
                message = self._git(worktree, "show", "-s", "--format=%B", source_commit).stdout.strip()
                trailer = f"(cherry picked from commit {source_commit})"
                # Commit the resolution, retaining source provenance even when
                # Git would otherwise omit -x after conflicts. This also handles
                # a deliberate empty resolution without resetting any files.
                self._git(worktree, "-c", "user.name=Orchestrator", "-c", "user.email=orchestrator@localhost", "commit", "--allow-empty", "-m", f"{message}\n\n{trailer}")
            elif staged:
                self._git(worktree, "-c", "user.name=Orchestrator", "-c", "user.email=orchestrator@localhost", "commit", "-m", f"Task {task_id}")
            commit = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            if worker_id == "integration" and metadata.get("pending_integration"):
                pending_info = metadata["pending_integration"]
                # Qwen can commit a resolution itself. Record the original
                # source commit only after HEAD advanced from the conflict base.
                if commit != pending_info["before_head"]:
                    source = pending_info["source_commit"]
                    if source not in metadata["integrated_commits"]:
                        metadata["integrated_commits"].append(source)
                    metadata.setdefault("resolved_integrations", {})[source] = commit
                    metadata.pop("pending_integration", None)
            metadata.setdefault("captures", {})[commit] = {"base_commit": info["base_commit"], "task_id": task_id, "worker_id": worker_id, "branch": info["branch"]}
            info.update({"commit_sha": commit, "active": False, "task_id": task_id, "patch_ref": f"artifact://{project_id}/patches/{task_id}.patch", "patch_path": str(patch_path)})
            self._save(path, metadata)
            return dict(info)

    def integrate(self, project_id: str, commit_shas: list[str]) -> dict:
        """Cherry-pick commits in order, leaving conflicts intact for Verify."""
        project_id = self._identifier(project_id)
        with self._locked(project_id) as (metadata, path):
            repository = self._registered(metadata)
            info = self._provision(metadata, project_id, "integration", None, None)
            worktree = Path(info["worktree_path"])
            self._save(path, metadata)

            def result(status: str) -> dict:
                return {**info, "status": status, "base_commit": self._git(worktree, "rev-parse", "HEAD").stdout.strip(), "integrated_commits": list(metadata["integrated_commits"]), "conflicts": self._git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()}

            if not self._clean(worktree):
                return result("CONFLICT" if self._git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.strip() else "DIRTY")
            if self._git(worktree, "rev-parse", "--verify", "CHERRY_PICK_HEAD", check=False).returncode == 0:
                return result("CONFLICT")
            history = self._git(worktree, "log", "--format=%B").stdout
            expanded = []
            for revision in commit_shas:
                if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
                    raise WorkspaceError("Integration requires a full Git commit SHA")
                tip = self._git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
                captured = metadata.get("captures", {}).get(tip)
                if captured:
                    base = captured["base_commit"]
                    if self._git(repository, "merge-base", "--is-ancestor", base, tip, check=False).returncode:
                        raise WorkspaceError("Captured task history no longer descends from its recorded baseline")
                    revisions = self._git(repository, "rev-list", "--reverse", "--topo-order", f"{base}..{tip}").stdout.splitlines()
                    # A no-op task still has a real baseline commit to record.
                    expanded.extend(revisions or [tip])
                else:
                    expanded.append(tip)
            for commit in dict.fromkeys(expanded):
                already_ancestor = self._git(worktree, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode == 0
                if commit not in metadata["integrated_commits"] and not already_ancestor and f"(cherry picked from commit {commit})" not in history:
                    metadata["pending_integration"] = {"source_commit": commit, "before_head": self._git(worktree, "rev-parse", "HEAD").stdout.strip()}
                    self._save(path, metadata)
                    picked = self._git(worktree, "-c", "user.name=Orchestrator", "-c", "user.email=orchestrator@localhost", "cherry-pick", "-x", commit, check=False)
                    if picked.returncode:
                        self._save(path, metadata)
                        return {**result("CONFLICT"), "failed_commit": commit}
                if commit not in metadata["integrated_commits"]:
                    metadata["integrated_commits"].append(commit)
                metadata.pop("pending_integration", None)
                self._save(path, metadata)
            metadata["workers"]["integration"]["base_commit"] = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            self._save(path, metadata)
            return result("SUCCESS")

    @staticmethod
    def verify_integrated(path: str | Path, original_commit: str, baseline: str) -> bool:
        """Prove ancestry or patch-equivalent task history in an integration tip.

        Patch IDs are historical evidence, not semantic acceptance tests. Conflict
        resolutions changing the patches fail closed and need explicit rework.
        """
        if not all(re.fullmatch(r"[0-9a-fA-F]{40}", value) for value in (original_commit, baseline)):
            return False
        repository = Path(path)
        if WorkspaceManager._git(repository, "merge-base", "--is-ancestor", original_commit, baseline, check=False).returncode == 0:
            return True
        common = WorkspaceManager._git(repository, "merge-base", original_commit, baseline, check=False)
        if common.returncode:
            return False
        fork = common.stdout.strip()
        source = WorkspaceManager._git(repository, "rev-list", f"{fork}..{original_commit}", check=False)
        target = WorkspaceManager._git(repository, "rev-list", f"{fork}..{baseline}", check=False)
        if source.returncode or target.returncode:
            return False

        def patch_id(revision: str) -> str | None:
            patch = WorkspaceManager._git(repository, "show", "--pretty=format:", "--binary", "--no-ext-diff", revision).stdout
            if not patch.strip():
                return None
            identity = subprocess.run(["git", "patch-id", "--stable"], input=patch, capture_output=True, text=True)
            if identity.returncode or not identity.stdout.strip():
                raise WorkspaceError("Unable to verify Git patch identity")
            return identity.stdout.split()[0]

        try:
            targets = {patch_id(revision) for revision in target.stdout.splitlines()}
            originals = {patch_id(revision) for revision in source.stdout.splitlines()}
            originals.discard(None)
            return originals.issubset(targets)
        except WorkspaceError:
            return False


# The earlier requirements use both names; keep one implementation.
WorktreeManager = WorkspaceManager
