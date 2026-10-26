"""Disposable coding CLI containers; LangGraph itself stays on the host."""
import os
from pathlib import Path
import subprocess

from .cli_logs import run_logged

FORWARDED_ENV = (
    'OPENAI_BASE_URL', 'OPENAI_MODEL', 'OPENAI_API_KEY', 'INTERNAL_LLM_API_KEY',
    'OPENCODE_CONFIG_CONTENT', 'OPENCODE_PERMISSION', 'SSL_CERT_FILE',
    'NODE_EXTRA_CA_CERTS', 'TMPDIR', 'AGENT_WORKDIR', 'AGENT_PROJECT_DIR',
)


def run_in_docker(command, *, docker, workdir, project, env, timeout, log_info=None, stream_format=None):
    cidfile = workdir / 'container.cid'
    docker_command = [docker, 'run', '--rm', '--init', '--network', 'host',
        '--cidfile', str(cidfile), '--workdir', str(workdir),
        '--mount', f'type=bind,src={workdir},dst={workdir}',
        '--mount', f'type=bind,src={project},dst={project}']
    mounted = {str(workdir), str(project)}
    if (project / '.git').is_file():
        metadata = subprocess.run(['git', '-C', str(project), 'rev-parse',
            '--path-format=absolute', '--git-common-dir'], capture_output=True,
            text=True, timeout=10, check=True)
        common = Path(metadata.stdout.strip()).resolve(strict=True)
        if str(common) not in mounted:
            docker_command += ['--mount', f'type=bind,src={common},dst={common}']
            mounted.add(str(common))

    for key in ('SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS'):
        value = env.get(key)
        if value:
            certificate = Path(value).expanduser().resolve(strict=True)
            env[key] = str(certificate)
            if str(certificate) not in mounted:
                docker_command += ['--mount', f'type=bind,src={certificate},dst={certificate},readonly']
                mounted.add(str(certificate))
    # Pass variable names only: API keys never enter the command line.
    for key in FORWARDED_ENV:
        if key in env:
            docker_command += ['--env', key]
    docker_command += [os.getenv('CODING_CLI_IMAGE', 'coding-agent-cli:local'), *command]
    try:
        return run_logged(docker_command, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, check=False, log_info=log_info, stream_format=stream_format)
    finally:
        # Killing docker run on timeout alone does not reliably stop the container.
        if cidfile.exists():
            container_id = cidfile.read_text().strip()
            if container_id:
                subprocess.run([docker, 'rm', '--force', container_id],
                    capture_output=True, timeout=30, check=False)
