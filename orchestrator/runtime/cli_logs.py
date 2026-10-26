"""Stream CLI output to persistent logs while preserving stdout/stderr results."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Lock, Thread
from uuid import uuid4
from .logging import event
from .cli_events import EventRenderer


def log_directory(value=None):
    return Path(value or os.getenv('CLI_LOG_DIR') or Path(__file__).resolve().parents[2] / '.local' / 'cli-logs').expanduser().resolve()


class StreamRedactor:
    def __init__(self, secrets):
        # Tool inputs/errors may contain JSON-escaped strings after rendering.
        self.secrets = sorted({form for value in secrets if value
                               for form in (value, json.dumps(value, ensure_ascii=False)[1:-1])},
                              key=len, reverse=True)
        self.pending = ''

    def feed(self, text, *, final=False):
        self.pending += text
        end = len(self.pending)
        if not final:
            # Retain only a suffix which could actually become a secret.
            # Holding max(secret_length) characters delays ordinary short logs.
            for secret in self.secrets:
                for size in range(min(len(secret) - 1, len(self.pending)), 0, -1):
                    if self.pending.endswith(secret[:size]):
                        end = min(end, len(self.pending) - size)
                        break
            for secret in self.secrets:
                start = self.pending.find(secret)
                while start >= 0:
                    if start < end < start + len(secret):
                        end = start
                    start = self.pending.find(secret, start + 1)
        text, self.pending = self.pending[:end], self.pending[end:]
        for secret in self.secrets:
            text = text.replace(secret, '[REDACTED]')
        return text


def _save_session(path, session):
    # Readers always see a complete metadata snapshot; never a partial JSON write.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(session, stream, ensure_ascii=False)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_logged(command, *, env, timeout, log_info=None, stream_format=None, **kwargs):
    # Keep the original runner when logging was explicitly left unconfigured.
    if not env.get('CLI_LOG_DIR'):
        return subprocess.run(command, env=env, timeout=timeout, **kwargs)
    folder = log_directory(env['CLI_LOG_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex}.log"
    secrets = sorted({v for k, v in env.items() if v and
                      any(word in k.upper() for word in ('TOKEN', 'API_KEY', 'PASSWORD', 'SECRET'))}, key=len, reverse=True)
    info = log_info or {}
    title = StreamRedactor(secrets).feed(str(info.get('title') or 'Phiên CLI'), final=True)
    session = {'name': name, 'title': ' '.join(title.split())[:160],
               'role': StreamRedactor(secrets).feed(str(info.get('role') or ''), final=True)[:32],
               'task_id': StreamRedactor(secrets).feed(str(info.get('task_id') or ''), final=True)[:256],
               'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
    metadata_path = folder / (name + '.json')
    outputs = [bytearray(), bytearray()]
    lock = Lock()
    for key in ('capture_output', 'text', 'encoding', 'errors', 'check'):
        kwargs.pop(key, None)
    event('cli.log_file', path=folder / name)
    with (folder / name).open('x', encoding='utf-8') as log:
        os.chmod(folder / name, 0o600)
        log.write(f"[{session['role'] or 'CLI'}] {session['title']}\nĐang khởi động CLI…\n")
        log.flush()
        _save_session(metadata_path, session)
        def copy(stream, result, *, render=False):
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            redactor = StreamRedactor(secrets)
            renderer = EventRenderer(stream_format) if render and stream_format else None
            def emit(text, *, final=False):
                if renderer:
                    text = renderer.feed(text, final=final)
                text = redactor.feed(text, final=final)
                with lock:
                    log.write(text)
                    log.flush()
            try:
                while chunk := stream.read1(4096):
                    result.extend(chunk)
                    emit(decoder.decode(chunk))
                emit(decoder.decode(b'', final=True), final=True)
            finally:
                stream.close()
        process = None
        threads = []
        try:
            process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
            threads = [Thread(target=copy, args=(stream, result), kwargs={'render': index == 0}, daemon=True)
                       for index, (stream, result) in enumerate(zip((process.stdout, process.stderr), outputs))]
            for thread in threads:
                thread.start()
            process.wait(timeout=timeout)
            session['status'] = 'completed' if process.returncode == 0 else 'failed'
            session['returncode'] = process.returncode
        except BaseException as exc:
            session['status'] = 'timed_out' if isinstance(exc, subprocess.TimeoutExpired) else 'failed'
            if process is not None:
                process.kill()
                process.wait()
            raise
        finally:
            for thread in threads:
                thread.join()
            session['ended_at'] = datetime.now(timezone.utc).isoformat()
            labels = {'completed': 'Đã xong', 'failed': 'Lỗi', 'timed_out': 'Quá thời gian'}
            log.write(f"\n[{labels[session['status']]}]\n")
            log.flush()
            _save_session(metadata_path, session)
        return subprocess.CompletedProcess(command, process.returncode,
            *(bytes(value).decode('utf-8', errors='replace') for value in outputs))
