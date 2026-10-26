"""Run OpenCode or Qwen Code against an internal OpenAI-compatible API."""

import json
import os
from pathlib import Path
import shutil
import subprocess

from .cli_logs import run_logged
from .cli_events import report_text
from typing import Literal
from urllib.parse import urlsplit

from .workdir import coding_workdir
from .docker_cli import run_in_docker
from .thinking import thinking_body
from .cli_skills import configured_skills, stage_skills
from .logging import log_cli, event


@log_cli
def run_llm_cli(
    prompt: str,
    cli: Literal["opencode", "qwen"] = "qwen",
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    cwd: str | Path | None = None,
    timeout: float = 1800,
    thinking: bool | None = None,
    thinking_format: str | None = None,
    role: Literal['plan', 'execute', 'reviewer'] | None = None,
    task_title: str | None = None,
    task_id: str | None = None,
) -> str:
    """Return the assistant report; raise on missing config, failure or timeout.

    Configuration defaults to QWEN_BASE_URL, QWEN_MODEL and QWEN_API_KEY.
    Load .env in the caller if needed (e.g. python-dotenv's load_dotenv()).
    base_url must include the API prefix, usually /v1. For servers without
    authentication, pass api_key="local". Tool approvals are always automatic.
    Each invocation gets a disposable cwd with project/ pointing to the real
    workspace. Only scratch files are cleaned; project edits persist.
    Native events stream into the monitor; task_title/task_id label that session.
    """
    if cli not in {"opencode", "qwen"}:
        raise ValueError("cli must be 'opencode' or 'qwen'")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    base_url = (base_url or os.getenv("QWEN_BASE_URL", "")).strip()
    model = (model or os.getenv("QWEN_MODEL", "")).strip()
    api_key = api_key if api_key is not None else os.getenv("QWEN_API_KEY", "")
    if not base_url or not model:
        raise ValueError("Provide base_url/model or set QWEN_BASE_URL/QWEN_MODEL")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an HTTP(S) API URL")
    runtime = os.getenv('CODING_CLI_RUNTIME', 'local')
    if runtime not in {'local', 'docker'}:
        raise ValueError('CODING_CLI_RUNTIME must be local or docker')
    docker = shutil.which('docker') if runtime == 'docker' else None
    executable = cli if runtime == 'docker' else shutil.which(cli)
    if runtime == 'docker' and docker is None:
        raise FileNotFoundError('Docker is required for CODING_CLI_RUNTIME=docker')
    if executable is None:
        raise FileNotFoundError(f"{cli} CLI is not installed or is missing from PATH")

    skills = configured_skills(cli, role=role)
    request_thinking = thinking_body(thinking=thinking, thinking_format=thinking_format)
    env = os.environ.copy()
    # Keep credentials out of argv, logs, and persistent configuration files.
    env["INTERNAL_LLM_API_KEY"] = api_key or "local"
    if cli == "qwen":
        env.update(
            OPENAI_BASE_URL=base_url,
            OPENAI_MODEL=model,
            OPENAI_API_KEY=api_key or "local",
        )
        command = [executable, "--auth-type", "openai", "--model", model,
                   "--yolo", "--output-format", "stream-json", "--include-partial-messages", "-p", prompt]
    else:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": ["internal"],
            "permission": "allow",
            "provider": {
                "internal": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Internal LLM",
                    "options": {
                        "baseURL": base_url,
                        "apiKey": "{env:INTERNAL_LLM_API_KEY}",
                    },
                    "models": {model: {"name": model, "options": request_thinking}},
                },
            },
        })
        env["OPENCODE_PERMISSION"] = json.dumps({"*": "allow"})
        command = [executable, "run", "--format", "json", "--model", f"internal/{model}", "--", prompt]

    log_info = {'title': task_title or {'plan': 'Khảo sát và lập kế hoạch',
                'execute': 'Triển khai nhiệm vụ', 'reviewer': 'Kiểm tra nhiệm vụ'}.get(role, 'Phiên CLI'),
                'role': (role or cli).capitalize(), 'task_id': task_id or ''}

    with coding_workdir(cwd, cli=cli) as (workdir, project):
        stage_skills(cli, workdir, skills)
        event('cli.prepared', cli=cli, role=role, skill_count=len(skills),
              skills=','.join(skill.name for skill in skills), workdir=workdir, project=project)
        if cli == 'qwen':
            settings_dir = workdir / '.qwen'
            settings_dir.mkdir(exist_ok=True)
            (settings_dir / 'settings.json').write_text(json.dumps({
                'model': {'generationConfig': {'extra_body': request_thinking}},
            }), encoding='utf-8')
        invocation_prompt = (
            f"Thư mục chạy riêng: {workdir}. Repository thật: {project}.\n"
            "project/ là liên kết tới checkout được giao; chỉ thao tác trong quyền "
            "của vai trò nêu dưới đây, "
            "đọc hướng dẫn repository trước khi làm việc. Với lệnh test/build/git, "
            "đặt working directory vào project/. "
            "Dùng tmp/ cho file tạm. Thư mục chạy riêng sẽ được dọn khi CLI kết thúc; "
            "không lưu kết quả cần giữ ngoài repository.\n\n" + prompt
        )
        if runtime == 'docker':
            invocation_prompt += "\nCLI đang chạy trong Docker: dùng python của image, không dùng .venv của host."
        command[-1] = invocation_prompt
        env["TMPDIR"] = str(workdir / "tmp")
        env["AGENT_WORKDIR"] = str(workdir)
        env["AGENT_PROJECT_DIR"] = str(project)
        if runtime == 'docker':
            result = run_in_docker(command, docker=docker, workdir=workdir, project=project,
                                   env=env, timeout=timeout, log_info=log_info, stream_format=cli)
        else:
            result = run_logged(
                command,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                log_info=log_info,
                stream_format=cli,
                check=False,
            )
    event('cli.process_exit', cli=cli, returncode=result.returncode,
          stdout_chars=len(result.stdout or ''), stderr_chars=len(result.stderr or ''))
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if api_key:
            detail = detail.replace(api_key, "[REDACTED]")
        raise RuntimeError(f"{cli} exited with code {result.returncode}: {detail}")
    try:
        return report_text(result.stdout, cli)
    except RuntimeError as exc:
        detail = str(exc).replace(api_key, '[REDACTED]') if api_key else str(exc)
        raise RuntimeError(f'{cli}: {detail}') from None
