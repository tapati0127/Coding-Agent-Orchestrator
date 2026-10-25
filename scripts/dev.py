"""Start local dev: .venv/bin/python scripts/dev.py [langgraph dev options]."""
import os
from pathlib import Path
import shutil
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    load_dotenv(ROOT / '.env')
    for key in ('SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS'):
        value = os.environ.get(key) or '.local/certs/ca-bundle.pem'
        certificate = Path(value).expanduser()
        if not certificate.is_absolute():
            certificate = ROOT / certificate
        certificate = certificate.resolve(strict=True)
        os.environ[key] = str(certificate)
    workspace = Path(os.environ.get('WORKSPACE_ROOT') or ROOT).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('WORKSPACE_ROOT must be a directory on the host')
    os.environ['WORKSPACE_ROOT'] = str(workspace)
    os.environ['PATH'] = os.pathsep.join([
        str(Path(sys.executable).parent), str(Path.home() / '.local/bin'), os.environ.get('PATH', ''),
    ])
    executable = shutil.which('langgraph')
    if not executable:
        raise SystemExit('langgraph CLI missing; install pip install -e ".[dev]"')
    if os.environ.get('CODING_CLI_RUNTIME', 'local') == 'docker':
        if not shutil.which('docker'):
            raise SystemExit('Docker is required for coding CLI containers')
        print('Coding CLIs in Docker: ' + os.environ.get('CODING_CLI_IMAGE', 'coding-agent-cli:local'), flush=True)
    else:
        for cli in ('qwen', 'opencode'):
            print(f'{cli}: {shutil.which(cli) or "not found in host PATH"}', flush=True)
    os.execv(executable, [executable, 'dev', '--config', str(ROOT / 'langgraph.json'), '--no-browser', *sys.argv[1:]])


if __name__ == '__main__':
    main()
