"""Stage per-CLI skill selections into disposable native discovery directories."""
import os
from pathlib import Path
import re
import shutil
import yaml

CONFIG_ROOT = Path(__file__).resolve().parents[2] / 'config' / 'cli'


def configured_skills(cli, config_path=None, *, role=None):
    """Resolve paths relative to the selected YAML; never load whole skills into prompts."""
    if cli not in {'qwen', 'opencode'}:
        raise ValueError('Unsupported CLI for skills')
    config = Path(config_path or os.getenv(f'{cli.upper()}_SKILLS_CONFIG') or CONFIG_ROOT / f'{cli}.yaml').expanduser().resolve(strict=True)
    data = yaml.safe_load(config.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) - {'skills'}:
        raise ValueError(f'{config}: expected a skills mapping')
    skills = data.get('skills', {})
    if not isinstance(skills, dict) or set(skills) - {'enabled', 'paths', 'roles'}:
        raise ValueError(f'{config}: skills accepts only enabled, paths and roles')
    enabled, paths = skills.get('enabled', True), skills.get('paths', [])
    if not isinstance(enabled, bool):
        raise ValueError(f'{config}: skills.enabled must be boolean')
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
        raise ValueError(f'{config}: skills.paths must be a list of nonempty paths')
    roles = skills.get('roles', {})
    if not isinstance(roles, dict) or set(roles) - {'plan', 'execute', 'reviewer'}:
        raise ValueError(f'{config}: skills.roles accepts plan, execute and reviewer')
    for name, entries in roles.items():
        if not isinstance(entries, list) or any(not isinstance(p, str) or not p.strip() for p in entries):
            raise ValueError(f'{config}: skills.roles.{name} must be a list of nonempty paths')
    if role is not None and role not in {'plan', 'execute', 'reviewer'}:
        raise ValueError('Skill role must be plan, execute or reviewer')
    if not enabled:
        return []
    paths = [*paths, *roles.get(role, [])]
    selected = {}
    for entry in paths:
        path = Path(entry).expanduser()
        path = (config.parent / path if not path.is_absolute() else path).resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f'Skill path must be a directory: {path}')
        candidates = [path] if (path / 'SKILL.md').is_file() else sorted(
            p for p in path.iterdir() if p.is_dir() and (p / 'SKILL.md').is_file())
        if not candidates:
            raise ValueError(f'No SKILL.md found in skill path: {path}')
        for candidate in candidates:
            text = (candidate / 'SKILL.md').read_text(encoding='utf-8')
            lines = text.splitlines()
            if not lines or lines[0].strip() != '---' or '---' not in lines[1:]:
                raise ValueError(f'Skill must have YAML frontmatter: {candidate}')
            end = lines.index('---', 1)
            metadata = yaml.safe_load('\n'.join(lines[1:end]))
            name = metadata.get('name') if isinstance(metadata, dict) else None
            description = metadata.get('description') if isinstance(metadata, dict) else None
            if (not isinstance(name, str) or len(name) > 64
                    or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
                    or name != candidate.name):
                raise ValueError(f'Skill name must match its directory and use lowercase/hyphens: {candidate}')
            if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                raise ValueError(f'Skill requires a description of 1-1024 characters: {candidate}')
            if name in selected and selected[name] != candidate:
                raise ValueError(f'Duplicate skill name {name!r} in {config}')
            selected[name] = candidate
    return list(selected.values())


def stage_skills(cli, workdir, skills):
    """Copy dependencies too, so skills work inside Docker without extra mounts."""
    target = Path(workdir) / f'.{cli}' / 'skills'
    for source in skills:
        shutil.copytree(source, target / source.name)
