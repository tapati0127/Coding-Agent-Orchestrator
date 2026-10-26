"""Stream CLI output to persistent logs while preserving stdout/stderr results."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from threading import Lock, Thread
from uuid import uuid4


def log_directory():
    return Path(os.getenv('CLI_LOG_DIR') or Path(__file__).resolve().parents[2] / '.local' / 'cli-logs').expanduser().resolve()


def run_logged(command, *, env, timeout, **kwargs):
    # Keep the original runner when logging was explicitly left unconfigured.
    if not env.get('CLI_LOG_DIR'):
        return subprocess.run(command, env=env, timeout=timeout, **kwargs)
    folder = log_directory()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex}.log"
    secrets = sorted({v for k, v in env.items() if v and
                      any(word in k.upper() for word in ('TOKEN', 'API_KEY', 'PASSWORD', 'SECRET'))}, key=len, reverse=True)
    keep = max(map(len, secrets), default=1) - 1
    outputs = [bytearray(), bytearray()]
    lock = Lock()
    for key in ('capture_output', 'text', 'encoding', 'errors', 'check'):
        kwargs.pop(key, None)
    with (folder / name).open('x', encoding='utf-8') as log:
        os.chmod(folder / name, 0o600)
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        def copy(stream, result):
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            pending = ''
            def emit(final=False):
                nonlocal pending
                end = len(pending) if final else max(0, len(pending) - keep)
                # Do not split a secret at the emission boundary.
                for secret in secrets:
                    start = pending.find(secret)
                    while start >= 0:
                        if start < end < start + len(secret):
                            end = start
                        start = pending.find(secret, start + 1)
                text, pending = pending[:end], pending[end:]
                for secret in secrets:
                    text = text.replace(secret, '[REDACTED]')
                with lock:
                    log.write(text)
                    log.flush()
            try:
                while chunk := stream.read1(4096):
                    result.extend(chunk)
                    pending += decoder.decode(chunk)
                    emit()
                pending += decoder.decode(b'', final=True)
                emit(final=True)
            finally:
                stream.close()
        threads = [Thread(target=copy, args=(stream, result), daemon=True)
                   for stream, result in zip((process.stdout, process.stderr), outputs)]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            for thread in threads:
                thread.join()
        return subprocess.CompletedProcess(command, process.returncode,
            *(bytes(value).decode('utf-8', errors='replace') for value in outputs))
