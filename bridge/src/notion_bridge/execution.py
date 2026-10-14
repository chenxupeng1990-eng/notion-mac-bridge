"""Run shell jobs with private, lossless logs and bounded output previews."""

from __future__ import annotations

import asyncio
import codecs
import os
from pathlib import Path
import re
import signal
import time
from typing import TypeVar


DEFAULT_LOG_DIR = Path(
    os.environ.get("NOTION_EXEC_LOG_DIR")
    or Path(os.environ.get("NOTION_WORKSPACE_DIR", str(Path.home() / "notion-workspace")))
    / "tasks" / "exec-logs"
)
_READ_SIZE = 64 * 1024
_JOB_ID = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_T = TypeVar("_T")


def _validate_cap(max_output_chars: int) -> None:
    if (
        isinstance(max_output_chars, bool)
        or not isinstance(max_output_chars, int)
        or not 1 <= max_output_chars <= 1_000_000
    ):
        raise ValueError("max_output_chars must be an integer from 1 to 1000000")


def _read_output(log_path: Path, max_output_chars: int) -> tuple[str, int]:
    """Retain at most the cap plus one decoded chunk while scanning the log.

    Counts are Unicode code points after UTF-8 decoding with replacement for
    invalid bytes. The raw log is never modified. Odd caps give the head the
    extra character; the truncation marker does not count toward the cap.
    """
    head_limit = (max_output_chars + 1) // 2
    tail_limit = max_output_chars // 2
    head_parts: list[str] = []
    head_chars = 0
    tail = ""
    total_chars = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def retain(text: str) -> None:
        nonlocal head_chars, tail, total_chars
        total_chars += len(text)
        if head_chars < head_limit:
            prefix = text[: head_limit - head_chars]
            head_parts.append(prefix)
            head_chars += len(prefix)
        if tail_limit:
            tail = (tail + text)[-tail_limit:]

    with log_path.open("rb") as log:
        while raw := log.read(_READ_SIZE):
            retain(decoder.decode(raw))
        retain(decoder.decode(b"", final=True))

    head = "".join(head_parts)
    if total_chars <= max_output_chars:
        remaining = total_chars - head_chars
        return head + (tail[-remaining:] if remaining else ""), total_chars

    omitted = total_chars - max_output_chars
    marker = f"[... 截断 {omitted} 字符，完整输出见 {log_path} ...]"
    return head + marker + tail, total_chars


def format_output(log_path: Path, max_output_chars: int) -> str:
    """Return a bounded first/last UTF-8 preview of an existing log file."""
    _validate_cap(max_output_chars)
    output, _ = _read_output(log_path, max_output_chars)
    return output


async def _await_uninterrupted(task: asyncio.Task[_T]) -> _T:
    """Finish resource cleanup even if the caller is cancelled again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    # Signal the group even if its shell has already exited: descendants can
    # still be alive. SIGKILL also stops children that ignore SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _cleanup(process: asyncio.subprocess.Process) -> None:
    await _await_uninterrupted(asyncio.create_task(_kill_and_reap(process)))


async def run_command(
    cmd: str,
    cwd: str,
    job_id: str,
    max_output_chars: int = 4000,
    timeout_seconds: int | float = 120,
    log_dir: Path | None = None,
) -> dict:
    """Run an authorized shell command in its own process group.

    The result contains ``code`` (the subprocess exit code), ``output``,
    ``log_path`` (absolute string), ``timed_out``, ``total_chars``, and
    ``duration_ms``. Timeout kills the whole group and returns partial output;
    cancellation kills the group, reaps the shell, preserves the log, and
    re-raises CancelledError. Callers can await this helper directly or schedule
    it as a background task in their job registry.
    """
    _validate_cap(max_output_chars)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 86_400
    ):
        raise ValueError("timeout_seconds must be a number greater than 0 and at most 86400")
    if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
        raise ValueError("job_id must contain only ASCII letters, digits, underscores, or hyphens")

    started = time.monotonic()
    directory = (DEFAULT_LOG_DIR if log_dir is None else log_dir).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = directory / f"{job_id}.log"
    # O_EXCL refuses both an existing log and a pre-existing symlink; no job
    # can overwrite another job's log. Neither stream ever enters a PIPE.
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    timed_out = False
    process: asyncio.subprocess.Process | None = None
    with os.fdopen(descriptor, "wb", buffering=0) as log:
        # Shield creation so cancellation cannot lose a newly spawned process
        # before we obtain the handle needed to kill and reap it.
        spawn = asyncio.create_task(
            asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd or None,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(spawn)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except TimeoutError:
                timed_out = True
                await _cleanup(process)
        except BaseException:
            if process is None:
                try:
                    process = await _await_uninterrupted(spawn)
                except (Exception, asyncio.CancelledError):
                    # Process creation failed; preserve the original exception.
                    pass
            if process is not None:
                await _cleanup(process)
            raise

    duration_ms = int((time.monotonic() - started) * 1000)
    output, total_chars = _read_output(log_path, max_output_chars)
    return {
        "code": process.returncode,
        "output": output,
        "log_path": str(log_path),
        "timed_out": timed_out,
        "total_chars": total_chars,
        "duration_ms": duration_ms,
    }
