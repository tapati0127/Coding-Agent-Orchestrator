from pathlib import Path
import signal
import subprocess
import sys

import pytest

from orchestrator.config import Settings
from orchestrator.supervisor import Child, Supervisor, child_commands, prepare_persistence, server_command


def test_process_topology_has_one_worker_per_role_and_five_execute_slots():
    commands = child_commands(Settings())
    assert len(commands) == 11  # Dispatcher + five single-role workers + five Execute.
    assert len([name for name, _ in commands if name.startswith("execute-")]) == 5
    assert [name for name, _ in commands].count("dispatcher") == 1
    assert not any("uvicorn" in arg for _, args in commands for arg in args)


def test_server_explicitly_overrides_pinned_cli_serial_concurrency_default():
    command = server_command(Settings(max_qwen_sessions=5))
    index = command.index("--n-jobs-per-worker")
    assert int(command[index + 1]) >= 5
    assert "--no-reload" in command


def test_persistence_is_repeatable_and_preserves_originals(tmp_path):
    cwd = tmp_path / "app"
    data = tmp_path / "data"
    original = cwd / ".langgraph_api"
    original.mkdir(parents=True)
    (original / "store.pckl").write_bytes(b"example-store-bytes")
    target = prepare_persistence(cwd, data)
    assert original.is_symlink()
    assert (target / "store.pckl").read_bytes() == b"example-store-bytes"
    assert len(list(cwd.glob(".langgraph_api.backup-*"))) == 1
    assert prepare_persistence(cwd, data) == target


def test_persistence_refuses_to_merge_two_existing_runtime_stores(tmp_path):
    cwd = tmp_path / "app"
    (cwd / ".langgraph_api").mkdir(parents=True)
    (cwd / ".langgraph_api" / "store.pckl").write_text("original")
    target = tmp_path / "data" / "langgraph"
    target.mkdir(parents=True)
    (target / "store.pckl").write_text("other")
    with pytest.raises(RuntimeError, match="Both runtime directories"):
        prepare_persistence(cwd, tmp_path / "data")
    assert (cwd / ".langgraph_api" / "store.pckl").read_text() == "original"
    assert (target / "store.pckl").read_text() == "other"


def test_persistence_refuses_replacing_an_existing_symlink(tmp_path):
    cwd = tmp_path / "app"
    cwd.mkdir()
    wrong = tmp_path / "another"
    wrong.mkdir()
    (cwd / ".langgraph_api").symlink_to(wrong, target_is_directory=True)
    with pytest.raises(RuntimeError, match="another data directory"):
        prepare_persistence(cwd, tmp_path / "data")


def test_shutdown_forwards_sigterm_and_reaps_processes(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    supervisor = Supervisor(Settings(), cwd=tmp_path)
    supervisor.children.append(Child("worker", [sys.executable], process))
    supervisor.shutdown()
    assert process.returncode == -signal.SIGTERM
    assert supervisor.children == []


def test_missing_auth_stops_before_opening_server_port(tmp_path):
    supervisor = Supervisor(Settings(), cwd=tmp_path)
    with pytest.raises(RuntimeError, match="distinct ORCHESTRATOR_TOKEN"):
        supervisor.run()
    assert supervisor.children == []
