"""Authenticated immediate RPC for Miaoda; never creates model queue tasks."""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from starlette.responses import JSONResponse
from .execution import run_command

# All locations are environment-overridable so this file holds no machine-specific paths.
WORKSPACE = Path(os.environ.get('NOTION_WORKSPACE_DIR', str(Path.home() / 'notion-workspace')))
# fallback: <repo root>/.env (bridge loads its own bridge/.env into the process
# environment, so the env-var path above is usually enough already)
TOKEN_PATH = Path(os.environ.get('MIAODA_ENV_PATH',
                                 str(Path(__file__).resolve().parents[3] / '.env')))
_EXECUTIONS: dict[str, tuple[float, str, asyncio.Task]] = {}


def _token() -> str:
    # Env first (e.g. systemd/launchd Environment), then the worker's .env file.
    direct = os.environ.get('MIAODA_WORKER_TOKEN', '').strip()
    if direct:
        return direct
    if not TOKEN_PATH.exists():
        return ''
    for line in TOKEN_PATH.read_text().splitlines():
        if line.startswith('MIAODA_WORKER_TOKEN='):
            return line.split('=', 1)[1].strip()
    return ''


def read_file(path: str, offset: int = 0, limit: int = 4000,
              task_root: str | None = None) -> dict:
    """Read character pages from workspace, legacy jobs, or a trusted task root.

    task_root is internal worker-authenticated context, never a public MCP
    argument. Roots do not authorize symlinks: every read component is opened
    relative to a verified directory descriptor with O_NOFOLLOW.
    """
    import codecs
    import errno
    import stat

    def check_path(value):
        try:
            text = os.fspath(value)
        except TypeError as exc:
            raise ValueError('path must be a nonempty string') from exc
        if not isinstance(text, str) or not text or '\0' in text:
            raise ValueError('path must be a nonempty string without NUL bytes')
        if '..' in text.split(os.sep):
            raise ValueError('parent traversal is not allowed')
        candidate = Path(text)
        if any(part.casefold() in ('.git', '.venv', 'audit.log')
               or part.casefold().startswith('.env') for part in candidate.parts):
            raise ValueError('sensitive path is blocked')
        return candidate

    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('offset must be nonnegative; limit must be1..100000')
    target = check_path(path).expanduser()
    if not target.is_absolute():
        target = WORKSPACE / target
    target = check_path(target)
    legacy_jobs = os.environ.get('NOTION_BRIDGE_JOBS_DIR')
    roots = [WORKSPACE.resolve()]
    if legacy_jobs:
        roots.append(Path(legacy_jobs).resolve())
    if task_root is not None:
        requested_root = check_path(task_root).expanduser()
        if not requested_root.is_absolute():
            raise ValueError('task_root must be an absolute directory')
        check_path(requested_root)
        resolved_root = check_path(requested_root.resolve(strict=True))
        if requested_root != resolved_root:
            raise ValueError('symlink task roots are not allowed')
        if not resolved_root.is_dir():
            raise ValueError('task_root must be an existing directory')
        roots.append(resolved_root)
    # Check both spellings. An outside alias resolving into a root must not
    # authorize an outside regular file substituted before descriptor traversal.
    if not any(target.is_relative_to(root) for root in roots):
        raise ValueError('read_file only allows Notion workspace, legacy jobs and trusted task roots')
    resolved = check_path(target.resolve(strict=True))
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError('read_file only allows Notion workspace, legacy jobs and trusted task roots')

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0)
    parent_fd = os.open(os.sep, directory_flags)
    file_fd = None
    sha = hashlib.sha256()
    total = 0
    selected = []
    try:
        try:
            for component in target.parts[1:-1]:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            file_fd = os.open(target.name, file_flags, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ValueError('symlinks and non-directory parents are not allowed') from exc
            raise
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError('read_file requires a regular file')
        stream = os.fdopen(file_fd, 'rb')
        file_fd = None  # The stream now owns and closes this descriptor.
        with stream:
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            for chunk in iter(lambda: stream.read(65536), b''):
                sha.update(chunk)
                text = decoder.decode(chunk)
                begin = max(0, offset - total)
                end = min(len(text), offset + limit - total)
                if end > begin:
                    selected.append(text[begin:end])
                total += len(text)
            text = decoder.decode(b'', final=True)
            if offset + limit > total and offset < total + len(text):
                selected.append(text[max(0, offset - total):offset + limit - total])
            total += len(text)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)
    content = ''.join(selected)
    return {'path': str(resolved), 'offset': offset, 'limit': limit, 'total_chars': total,
            'next_offset': offset + len(content), 'eof': offset + len(content) >= total,
            'content': content, 'sha256': sha.hexdigest()}


async def rpc(request):
    try:
        expected = _token()
        header = request.headers.get('authorization','')
        if not expected or not header.lower().startswith('bearer ') or not hmac.compare_digest(header[7:],expected):
            return JSONResponse({'error':'Invalid bridge credential'},status_code=401)
        if len(await request.body()) > 200000:
            return JSONResponse({'error':'Request too large'},status_code=413)
        body = await request.json()
        if not isinstance(body, dict): raise ValueError('request must be an object')
        args = body.get('params',{})
        if not isinstance(args,dict): raise ValueError('params must be an object')
        if body.get('method') == 'read_file':
            result = await asyncio.to_thread(read_file, args.get('path',''), args.get('offset',0), args.get('limit',4000), task_root=args.get('_task_root'))
            return JSONResponse(result)
        if body.get('method') != 'exec': raise ValueError('Unsupported direct method')
        cmd = args.get('cmd')
        if not isinstance(cmd,str) or not cmd.strip() or len(cmd)>50000: raise ValueError('Invalid cmd')
        cwd = Path(args.get('cwd') or WORKSPACE).expanduser().resolve(strict=True)
        if not cwd.is_dir(): raise ValueError('cwd must be an existing directory')
        timeout = args.get('timeout_seconds',120)
        cap = args.get('max_output_chars',4000)
        if type(timeout) is not int or not 1<=timeout<=86400 or type(cap) is not int or not 1<=cap<=1000000:
            raise ValueError('Invalid timeout or preview size')
        request_id = body.get('request_id') or uuid.uuid4().hex
        if not isinstance(request_id,str) or len(request_id)>100: raise ValueError('Invalid request_id')
        fingerprint=hashlib.sha256(json.dumps([cmd,str(cwd),timeout,cap],ensure_ascii=False).encode()).hexdigest()
        previous=_EXECUTIONS.get(request_id)
        if previous:
            if previous[1]!=fingerprint: raise ValueError('request_id belongs to different command')
            task=previous[2]
        else:
            # Keep task results briefly for safe retries after a lost response.
            for key,(created,_,pending) in list(_EXECUTIONS.items()):
                if pending.done() and time.monotonic()-created>3600: _EXECUTIONS.pop(key,None)
            if len(_EXECUTIONS)>=1000: raise ValueError('Too many direct executions')
            jid='job-'+uuid.uuid4().hex
            async def execute():
                result=await run_command(cmd,str(cwd),jid,max_output_chars=cap,timeout_seconds=timeout,
                                         log_dir=WORKSPACE/'tasks'/'exec-logs')
                return {'job_id':jid,**result}
            task=asyncio.create_task(execute())
            _EXECUTIONS[request_id]=(time.monotonic(),fingerprint,task)
        return JSONResponse(await asyncio.shield(task))
    except (ValueError,OSError,TypeError) as exc:
        return JSONResponse({'error':str(exc)},status_code=400)
