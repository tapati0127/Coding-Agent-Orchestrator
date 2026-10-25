"""Role-specific skill selection, work/decision isolation and secret handling."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.runtime.skills import load_work_skills
from orchestrator.schemas.task_graph import Agent


def config_file(tmp_path, agents=None, skills=None):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "skills": skills or {"gitlab-cli": "gitlab.md"},
                               "agents": agents or {"ANALYZE": ["gitlab-cli"]}}))
    return path


@pytest.mark.parametrize("agent", list(Agent))
def test_default_role_skills(agent, monkeypatch):
    monkeypatch.delenv("WORK_PASS_SKILLS_CONFIG", raising=False)
    loaded = load_work_skills(agent)
    if agent == Agent.ANALYZE:
        assert loaded.names == ("gitlab-cli",)
        assert "glab api --method GET" in loaded.instructions
        assert loaded.environment == ("GITLAB_HOST", "GITLAB_TOKEN")
    else:
        assert loaded.names == () and loaded.instructions == "" and loaded.environment == ()


def test_custom_config_uses_its_directory_and_role_specific_assignments(tmp_path, monkeypatch):
    config_dir = tmp_path / "custom"
    config_dir.mkdir()
    (config_dir / "plan.md").write_text("Use the team's planning conventions.")
    config = config_file(config_dir, {"PLAN_DELEGATE": ["planning"]}, {"planning": "plan.md"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORK_PASS_SKILLS_CONFIG", str(config))
    plan = load_work_skills("PLAN_DELEGATE")
    assert plan.names == ("planning",) and "planning conventions" in plan.instructions
    assert plan.environment == ()
    assert load_work_skills("ANALYZE").names == ()


@pytest.mark.parametrize("agents", [
    {"ANALYZE": ["unknown"]}, {"ANALYZE": ["gitlab-cli", "gitlab-cli"]}, {"UNKNOWN": []},
])
def test_invalid_assignments_fail_before_model_invocation(tmp_path, agents):
    config = config_file(tmp_path, agents)
    with pytest.raises(ValidationError):
        load_work_skills("ANALYZE", config)


def test_selected_missing_or_empty_skill_fails_instead_of_silently_disabling(tmp_path):
    config = config_file(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_work_skills("ANALYZE", config)
    (tmp_path / "gitlab.md").write_text(" \n")
    with pytest.raises(ValueError, match="empty instructions"):
        load_work_skills("ANALYZE", config)


def test_unselected_missing_skill_does_not_break_other_roles(tmp_path):
    assert load_work_skills("EXECUTE", config_file(tmp_path)).names == ()
