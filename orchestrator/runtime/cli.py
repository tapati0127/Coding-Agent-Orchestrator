"""Run OpenCode or Qwen Code against an internal OpenAI-compatible API."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Literal
from urllib.parse import urlsplit

from .workdir import coding_workdir
from .docker_cli import run_in_docker


def run_llm_cli(
    prompt: str,
    cli: Literal["opencode", "qwen"] = "qwen",
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    cwd: str | Path | None = None,
    timeout: float = 1800,
) -> str:
    """Return CLI stdout; raise on missing config, executable, failure or timeout.

    Configuration defaults to QWEN_BASE_URL, QWEN_MODEL and QWEN_API_KEY.
    Load .env in the caller if needed (e.g. python-dotenv's load_dotenv()).
    base_url must include the API prefix, usually /v1. For servers without
    authentication, pass api_key="local". Tool approvals are always automatic.
    Each invocation gets a disposable cwd with project/ pointing to the real
    workspace. Only scratch files are cleaned; project edits persist.
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
                   "--yolo", "--output-format", "text", "-p", prompt]
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
                    "models": {model: {"name": model}},
                },
            },
        })
        env["OPENCODE_PERMISSION"] = json.dumps({"*": "allow"})
        command = [executable, "run", "--model", f"internal/{model}", "--", prompt]

    with coding_workdir(cwd, cli=cli) as (workdir, project):
        invocation_prompt = (
            f"Thư mục chạy riêng: {workdir}. Repository thật: {project}.\n"
            "project/ là liên kết tới repository; đọc/sửa code ở repository thật, "
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
                                   env=env, timeout=timeout)
        else:
            result = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if api_key:
            detail = detail.replace(api_key, "[REDACTED]")
        raise RuntimeError(f"{cli} exited with code {result.returncode}: {detail}")
    return result.stdout.strip()
