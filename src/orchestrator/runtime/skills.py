"""Trusted, config-selected SKILL.md instructions for Work passes only.

The Orchestrator loads these explicitly because Qwen safe mode disables native
skill discovery. Never take skill paths or environment grants from model output.
"""
from dataclasses import dataclass
from importlib.resources import files
import os
from pathlib import Path
import re
from typing import Literal

from pydantic import model_validator

from orchestrator.schemas.task_graph import Agent, StrictModel

# Integration credentials are opt-in by selected skill, not a general environment
# passthrough. Store/Dispatcher credentials must never reach Qwen subprocesses.
_SKILL_ENVIRONMENT = {"gitlab-cli": ("GITLAB_HOST", "GITLAB_TOKEN")}
SKILL_SECRET_ENVIRONMENT = ("GITLAB_TOKEN",)


class WorkSkillsConfig(StrictModel):
    version: Literal[1]
    skills: dict[str, str]
    agents: dict[Agent, list[str]]

    @model_validator(mode="after")
    def validate_assignments(self):
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) for name in self.skills):
            raise ValueError("Invalid skill name")
        for names in self.agents.values():
            if len(names) != len(set(names)):
                raise ValueError("Duplicate skill assignment")
            if any(name not in self.skills for name in names):
                raise ValueError("Unknown skill assignment")
        return self


@dataclass(frozen=True)
class WorkSkills:
    names: tuple[str, ...]
    instructions: str
    environment: tuple[str, ...]


def default_config_path() -> Path:
    source = Path(__file__).resolve().parents[3] / "config" / "work_pass_skills.json"
    if source.is_file():
        return source
    return Path(str(files("orchestrator").joinpath("config", "work_pass_skills.json")))


def load_work_skills(agent: str, config_path: str | Path | None = None) -> WorkSkills:
    """Called in a worker thread; relative skill paths resolve against config."""
    path = Path(config_path or os.getenv("WORK_PASS_SKILLS_CONFIG") or default_config_path()).resolve()
    if path.stat().st_size > 65536:
        raise ValueError("Work skills config is too large")
    config = WorkSkillsConfig.model_validate_json(path.read_text(encoding="utf-8"))
    names = tuple(config.agents.get(Agent(agent), []))
    sections = []
    total_size = 0
    for name in names:
        skill_path = (path.parent / config.skills[name]).resolve()
        total_size += skill_path.stat().st_size
        if total_size > 131072:
            raise ValueError("Work skill instructions exceed 128 KiB")
        instructions = skill_path.read_text(encoding="utf-8").strip()
        if not instructions:
            raise ValueError(f"Skill {name} has empty instructions")
        sections.append(f"<work-skill name=\"{name}\">\n{instructions}\n</work-skill>")
    environment = tuple(dict.fromkeys(key for name in names for key in _SKILL_ENVIRONMENT.get(name, ())))
    return WorkSkills(names, "\n\n".join(sections), environment)
