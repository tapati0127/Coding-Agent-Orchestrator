from pathlib import Path
import signal
from unittest.mock import Mock

import pytest

from orchestrator.config import Settings
from orchestrator.supervisor import Supervisor, worker_specs, prepare_persistence, server_command


def test_process_topology_has_one_worker_per_role_and_five_execute_slots():
    roles = worker_specs(Settings())
    assert len(roles) == 10
    assert list(roles.values()).count("EXECUTE") == 5
    assert list(roles.values()).count("COMMUNICATOR") == 1


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


def wait_for_shutdown(inbox):
    assert inbox.get() is None


def test_shutdown_sends_queue_sentinel_and_joins_worker_process(tmp_path):
    supervisor = Supervisor(Settings(), cwd=tmp_path)
    inbox = supervisor.context.Queue()
    process = supervisor.context.Process(target=wait_for_shutdown, args=(inbox,))
    process.start()
    supervisor.inboxes["worker"] = inbox
    supervisor.children["worker"] = process
    supervisor.shutdown()
    assert process.exitcode == 0
    assert supervisor.children == {}


def test_missing_auth_stops_before_opening_server_port(tmp_path):
    supervisor = Supervisor(Settings(), cwd=tmp_path)
    with pytest.raises(RuntimeError, match="Set ORCHESTRATOR_TOKEN"):
        supervisor.run()
    assert supervisor.children == {}


def test_runtime_only_startup_does_not_require_dashboard_token(tmp_path, monkeypatch):
    reached = Mock(side_effect=OSError("Reached persistence initialization"))
    monkeypatch.setattr("orchestrator.supervisor.prepare_persistence", reached)
    supervisor = Supervisor(Settings(orchestrator_token="runtime-only"), cwd=tmp_path)
    with pytest.raises(OSError, match="Reached persistence"):
        supervisor.run()
    reached.assert_called_once()
