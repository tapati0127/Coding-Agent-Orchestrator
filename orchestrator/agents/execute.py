"""Execute role: implement with CLI, assess the natural-language report."""
from pathlib import Path
from langchain_openai import ChatOpenAI
from orchestrator.runtime.cli import run_llm_cli
from orchestrator.runtime.two_phase import CONFIG_ROOT
from orchestrator.schemas import ExecuteAssessment, ExecuteTask
from orchestrator.state import TaskState as ExecuteState
from .assessed import build_assessed_agent

DEFAULT_CONFIG = CONFIG_ROOT / 'execute.yaml'


def build_execute_agent(config_path: str | Path = DEFAULT_CONFIG, **llm_options):
    return build_assessed_agent(config_path, role='Execute', cli_runner=run_llm_cli,
                                chat_factory=ChatOpenAI, **llm_options)
