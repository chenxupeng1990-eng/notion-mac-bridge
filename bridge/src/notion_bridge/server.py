"""Notion cloud agent tools for local files, shell execution and rendering.

File tools enforce allowlists and sensitive-path exclusions. Writes use the
scenes and Notion workspace roots. Execution uses the current user's rights;
cwd is organizational, not a sandbox. MCP bearer authentication and the tunnel
protect access; Miaoda immediate RPC has independent worker authentication.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import os
import uuid
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import MCPServer, Image
from mcp_types import TextContent
from .discovery import CachedMCPServer
from .execution import run_command, format_output
from .file_tools import write_bytes, encode_preview, inspect_image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # notion-bridge/


# --------------------------------------------------------------------------
# config + secrets
# --------------------------------------------------------------------------
def _load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env(ROOT / ".env")

with open(ROOT / "config.json") as f:
    CFG = json.load(f)

ALLOWED_ROOTS = [os.path.realpath(os.path.expanduser(p)) for p in CFG["allowed_roots"]]
MAGPIE_BASE = CFG["magpie_base"].rstrip("/")
DELEGATE_MODEL = CFG["delegate_model"]
# expert lane: stronger local model (Kimi K3) for work that must run on this
# machine but still needs good taste — cloud Opus keeps the final word.
DELEGATE_EXPERT_MODEL = CFG.get("delegate_expert_model", DELEGATE_MODEL)
PORT = int(CFG.get("port", 8760))
MAX_READ_LINES = int(CFG.get("max_read_lines", 400))
MAX_SEARCH_RESULTS = int(CFG.get("max_search_results", 80))
DELEGATE_MAX_TURNS = int(CFG.get("delegate_max_turns", 12))
AUDIT_LOG = ROOT / "audit.log"
# kimi_exec: headless kimi-code CLI, execution only. cwd is pinned to JOBS_DIR,
# which is inside an allowed root; the CLI still runs with local user rights, so
# treat its output as untrusted and keep the bearer token + tunnel as the gate.
JOBS_DIR = os.path.realpath(os.path.expanduser(
    CFG.get("jobs_dir", str(ROOT / "jobs"))))
os.makedirs(JOBS_DIR, exist_ok=True)
KIMI_BIN = CFG.get("kimi_bin", "/opt/homebrew/bin/kimi")
KIMI_TIMEOUT = int(CFG.get("kimi_timeout", 300))
# -p is already non-interactive and kimi's config sets default_permission_mode
# = "auto", so no --auto/--yolo flag (they conflict with -p). Pin a model that
# works through magpie: the default codex-first route trips qfmodel's
# max_tokens cap; the K3 alias is verified.
KIMI_MODEL = CFG.get("kimi_model", "magpie/kimi-code-cn/k3")
# render: three.js/WebGL in headless Chrome. Writes are fenced to the scenes
# subtree; render() only ever invokes the fixed render.mjs script.
RENDERER_DIR = os.path.realpath(os.path.expanduser(
    CFG.get("renderer_dir", str(ROOT / "renderer"))))
SCENES_DIR = os.path.join(RENDERER_DIR, "scenes")
RENDERS_DIR = os.path.join(RENDERER_DIR, "..", "renders")
RENDER_SCRIPT = os.path.join(RENDERER_DIR, "render.mjs")
RENDER_TIMEOUT = int(CFG.get("render_timeout", 600))
_WRITE_EXTS = {".html", ".htm", ".js", ".mjs", ".json", ".css", ".glsl",
               ".vert", ".frag", ".md", ".txt"}
MAX_WRITE_BYTES = 200_000
MAX_BINARY_BYTES = int(CFG.get("max_binary_bytes", 50 * 1024 * 1024))
MAX_IMAGE_BYTES = int(CFG.get("max_image_bytes", 50 * 1024 * 1024))
WORKSPACE_DIR = os.path.realpath(os.path.expanduser(CFG.get("workspace_dir", "~/claude/notion-workspace")))
EXEC_LOG_DIR = Path(WORKSPACE_DIR) / "tasks" / "exec-logs"
GENERAL_PYTHON = str(Path(WORKSPACE_DIR) / ".venv" / "bin" / "python")
_WRITE_EXTS.update({".py", ".sh", ".yaml", ".yml", ".toml", ".csv"})
_FETCH_MAX_BYTES = int(CFG.get("fetch_max_bytes", 50 * 1024 * 1024))
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".glb", ".gltf",
    ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".bin",
    ".py", ".sh", ".yaml", ".yml", ".toml", ".csv",
    ".tgz", ".tar", ".gz", ".zip", ".pdf",
}
os.makedirs(SCENES_DIR, exist_ok=True)
# DNS-rebinding protection: the server binds to 127.0.0.1 and the real gate is
# the bearer token, so we can allow any Host when fronted by a tunnel. Set
# allow_any_host=false and list exact hosts in allowed_hosts to re-enable it.
ALLOW_ANY_HOST = bool(CFG.get("allow_any_host", True))
EXTRA_ALLOWED_HOSTS = list(CFG.get("allowed_hosts", []))

_TOKEN = os.environ.get("NOTION_BRIDGE_TOKEN", "")
if not _TOKEN:
    raise SystemExit("NOTION_BRIDGE_TOKEN missing from environment/.env")


# --------------------------------------------------------------------------
# security helpers
# --------------------------------------------------------------------------
_DENY_BASENAMES = {".env", "audit.log"}
_DENY_DIRNAMES = {".git"}


def _is_sensitive(rp: str) -> bool:
    base = os.path.basename(rp)
    if base in _DENY_BASENAMES or base.startswith(".env"):
        return True
    return any(part in _DENY_DIRNAMES for part in rp.split(os.sep))


def resolve_allowed(p: str) -> str:
    """Expand, resolve symlinks, and confirm the path is under an allowed root.
    Sensitive files (.env, audit.log) and .git contents are blocked even inside
    an allowed root — the bearer token in .env is the tunnel's only gate."""
    rp = os.path.realpath(os.path.expanduser(p))
    for root in ALLOWED_ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            if _is_sensitive(rp):
                raise ValueError(f"sensitive path blocked: {p}")
            return rp
    raise ValueError(f"path not under an allowed root: {p}")


def audit(event: str, detail: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event} {detail}\n"
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass


# --------------------------------------------------------------------------
# read-only primitives (shared by the MCP tools and the delegate loop)
# --------------------------------------------------------------------------
def _fs_list(path: str) -> str:
    rp = resolve_allowed(path)
    if not os.path.isdir(rp):
        return f"not a directory: {path}"
    entries = []
    with os.scandir(rp) as it:
        for e in it:
            if _is_sensitive(os.path.join(rp, e.name)):
                continue
            try:
                kind = "d" if e.is_dir() else "f"
                size = e.stat().st_size if e.is_file() else ""
            except OSError:
                kind, size = "?", ""
            entries.append(f"{kind} {size:>10} {e.name}")
    audit("fs_list", rp)
    if not entries:
        return "(empty)"
    return "\n".join(entries[:500])


def _fs_read(path: str, offset: int = 0) -> str:
    rp = resolve_allowed(path)
    if not os.path.isfile(rp):
        return f"not a file: {path}"
    with open(rp, "r", errors="replace") as f:
        lines = f.readlines()
    window = lines[offset : offset + MAX_READ_LINES]
    audit("fs_read", f"{rp} +{offset} x{len(window)}")
    out = "".join(f"{offset + i + 1}\t{ln}" for i, ln in enumerate(window))
    more = len(lines) - (offset + len(window))
    return out + (f"\n… {more} more lines (offset={offset + len(window)})" if more > 0 else "")


_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache",
              "dist", "build", ".pytest_cache", ".next"}
# search walks files directly, so it must apply the same sensitivity filter


def _fs_search(pattern: str, path: str, glob: str = "") -> str:
    import fnmatch
    import re
    rp = resolve_allowed(path)
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"[bad regex] {e}"
    if not os.path.isdir(rp):
        return f"not a directory: {path}"
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(rp):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if glob and not fnmatch.fnmatch(fn, glob):
                continue
            fp = os.path.join(dirpath, fn)
            if _is_sensitive(fp):
                continue
            try:
                with open(fp, "r", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            rel = os.path.relpath(fp, rp)
                            matches.append(f"{rel}:{i}:{line.rstrip()[:200]}")
                            if len(matches) >= MAX_SEARCH_RESULTS:
                                break
            except (OSError, UnicodeError):
                continue
        if len(matches) >= MAX_SEARCH_RESULTS:
            break
    audit("fs_search", f"{pattern!r} in {rp} glob={glob or '-'} -> {len(matches)}")
    return "\n".join(matches) if matches else "(no matches)"


# --------------------------------------------------------------------------
# magpie gateway client (Anthropic /v1/messages) with a read-only tool loop
# --------------------------------------------------------------------------
_DELEGATE_TOOLS = [
    {
        "name": "read",
        "description": "Read a text file under an allowed root. Returns numbered lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "0-based line offset"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": "ripgrep for a pattern under a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern", "path"],
        },
    },
]

_SYSTEM = (
    "You are a coding assistant working on a local machine through a bridge. "
    "You can only READ files (read) and SEARCH (search) under the allowed roots. "
    "You cannot write or run commands. Answer the task using the files you can see. "
    "Be concise. When you have enough information, give the final answer as plain text."
)

_SYSTEM_EXPERT = (
    "You are a senior engineer working on a local machine through a bridge. "
    "You can only READ files (read) and SEARCH (search) under the allowed roots. "
    "You cannot write or run commands. Give a careful, well-reasoned answer grounded "
    "in the actual file contents. Prefer correctness and clarity over speed. "
    "When you have enough information, give the final answer as plain text."
)


async def _magpie_messages(client: httpx.AsyncClient, body: dict) -> dict:
    r = await client.post(
        f"{MAGPIE_BASE}/v1/messages",
        json=body,
        headers={"x-api-key": "notion-bridge", "anthropic-version": "2023-06-01"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


async def _delegate_run(task: str, context_path: str, model: str, system: str) -> str:
    """Drive the local model through a bounded read-only tool loop.
    Falls back to a single turn (with pre-injected context) if the model
    does not use tools."""
    messages = [{"role": "user", "content": task}]
    used_tools = False
    async with httpx.AsyncClient() as client:
        for _ in range(DELEGATE_MAX_TURNS):
            body = {
                "model": model,
                "max_tokens": 2048,
                "system": system,
                "messages": messages,
                "tools": _DELEGATE_TOOLS,
            }
            try:
                data = await _magpie_messages(client, body)
            except httpx.HTTPError as e:
                # provider may reject the tools param -> retry once without it
                body.pop("tools", None)
                try:
                    data = await _magpie_messages(client, body)
                except httpx.HTTPError as e2:
                    return f"[magpie error] {e2}"

            blocks = data.get("content", [])
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]

            if not tool_uses:
                return "\n".join(texts).strip() or "(empty response)"

            used_tools = True
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for tu in tool_uses:
                name = tu.get("name")
                inp = tu.get("input", {}) or {}
                try:
                    if name == "read":
                        obs = _fs_read(inp.get("path", ""), int(inp.get("offset", 0)))
                    elif name == "search":
                        obs = _fs_search(inp.get("pattern", ""), inp.get("path", ""),
                                         inp.get("glob", ""))
                    else:
                        obs = f"unknown tool {name}"
                except ValueError as e:  # path rejected
                    obs = f"[rejected] {e}"
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.get("id", ""),
                    "content": obs[:6000],
                })
            messages.append({"role": "user", "content": results})

    return "[stopped: reached max turns without a final answer]"


async def _delegate(task: str, context_path: str = "", files: list[str] | None = None,
                    quality: str = "standard") -> str:
    if quality == "expert":
        model, system = DELEGATE_EXPERT_MODEL, _SYSTEM_EXPERT
    else:
        quality = "standard"  # normalize any other value
        model, system = DELEGATE_MODEL, _SYSTEM
    audit("delegate", f"q={quality} model={model} {task[:120]!r} ctx={context_path or '-'} files={files or '-'}")
    ctx = ""
    if context_path:
        try:
            ctx += f"\n[context: contents of {context_path}]\n{_fs_list(context_path)[:2000]}"
        except ValueError as e:
            return f"[rejected] {e}"
    for fp in files or []:
        try:
            ctx += f"\n[file: {fp}]\n{_fs_read(fp)[:4000]}"
        except ValueError as e:
            ctx += f"\n[file: {fp}]\n[rejected] {e}"
    return await _delegate_run(task + ctx, context_path, model, system)


# --------------------------------------------------------------------------
# MCP server + tools
# --------------------------------------------------------------------------
mcp = CachedMCPServer(
    "notion-bridge",
    instructions=(
        "Operate the user's Mac through file, shell, agent and rendering tools. "
        "Use fs_list/fs_read/fs_search for allowed files, delegate for read-only "
        "subtasks and kimi_exec for local agent execution. Use exec for known "
        "commands; inspect log_path for full output and timed_out for timeouts. "
        f"Write projects under {WORKSPACE_DIR}, passing "
        "absolute workspace paths; relative writes retain legacy scenes behavior. "
        "Shell commands have the user's permissions; cwd is not a sandbox."
    ),
)


@mcp.tool()
def fs_list(path: str) -> str:
    """List a directory under an allowed root. path is absolute or ~-prefixed."""
    try:
        return _fs_list(path)
    except ValueError as e:
        return f"[rejected] {e}"


@mcp.tool()
def fs_read(path: str, offset: int = 0) -> str:
    """Read a text file (numbered lines, capped). offset pages further in."""
    try:
        return _fs_read(path, offset)
    except ValueError as e:
        return f"[rejected] {e}"


@mcp.tool()
def fs_search(pattern: str, path: str, glob: str = "") -> str:
    """Regex search (case-insensitive) for a pattern under a directory.
    Optional glob to filter filenames (e.g. '*.py')."""
    try:
        return _fs_search(pattern, path, glob)
    except ValueError as e:
        return f"[rejected] {e}"


@mcp.tool()
async def delegate(task: str, context_path: str = "", files: list[str] | None = None,
                   quality: str = "standard") -> str:
    """Hand a self-contained subtask to a LOCAL model that answers using the allowed
    roots. Use the 'quality' lane to pick how strong the local model is:
      standard (default) — cheap/fast local model. Best for heavy reading, locating
        files, summarizing code, anything mechanical. You (the planner) keep the taste.
      expert — a stronger local model (Kimi K3). Use when real content or code must be
        produced ON this machine (the planner cannot reach the machine directly), but
        still review the result yourself.
    context_path seeds a directory listing; files pre-injects specific file contents
    (most reliable). The worker can also call read/search itself. Returns a summary."""
    return await _delegate(task, context_path, files, quality)


@mcp.tool()
async def kimi_exec(task: str, workdir: str = "") -> str:
    """Run a real EXECUTION task on this machine via the local kimi-code CLI in
    headless mode (`kimi -p <task>`, auto-approve, Kimi K3 model). Use this for
    anything that must actually run on the machine: writing files, running a
    build, installing deps, executing scripts, rendering, etc. — delegate() can
    only read.

    workdir: optional directory to run in. Defaults to the sandboxed jobs dir;
    any other directory must be inside an allowed root. The agent runs with
    auto-approval, so phrase the task as a complete instruction with an
    explicit, verifiable success criterion."""
    if workdir:
        try:
            cwd = resolve_allowed(workdir)
        except ValueError as e:
            return f"[rejected] {e}"
        if not os.path.isdir(cwd):
            return f"not a directory: {workdir}"
    else:
        cwd = JOBS_DIR
    audit("kimi_exec", f"cwd={cwd} task={task[:160]!r}")
    cmd = [KIMI_BIN, "-p", task, "-m", KIMI_MODEL, "--add-dir", cwd,
           "--output-format", "text"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=KIMI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        audit("kimi_exec", f"TIMEOUT after {KIMI_TIMEOUT}s")
        return f"[timeout] kimi did not finish within {KIMI_TIMEOUT}s"
    except FileNotFoundError:
        return f"[error] kimi CLI not found at {KIMI_BIN}"
    text = out.decode(errors="replace")
    audit("kimi_exec", f"exit={proc.returncode} bytes={len(text)}")
    return f"[exit {proc.returncode}]\n{text[-6000:]}"


@mcp.tool()
def view_image(path: str, max_width: int = 1024, format: str = "jpeg",
               mode: str = "inline") -> list:
    """Inspect or preview an allowed image without altering its original.
    For Notion use mode=info: return only path, oriented dimensions, source format,
    byte size and frame count. mode=file saves a thumbnail under Notion workspace
    artifacts/image-previews/ and returns only metadata/path/SHA, never image data.
    max_width (default1024) and format (jpeg/png/webp) apply to previews, <=300KB.
    mode=inline preserves the original image-block behavior for compatible clients;
    Notion may expose it as costly encoded text rather than a visible image.
    A file path alone does not give Notion visual access. Supported input:
    png, jpg, jpeg, gif, webp under an allowed root."""
    try:
        if mode not in {"inline", "info", "file"}:
            raise ValueError("mode must be inline, info, or file")
        rp = resolve_allowed(path)
        if not os.path.isfile(rp):
            raise ValueError(f"not a file: {path}")
        if os.path.splitext(rp)[1].lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            raise ValueError(f"not an image: {path}")
        if os.path.getsize(rp) > MAX_IMAGE_BYTES:
            raise ValueError(f"source image exceeds {MAX_IMAGE_BYTES} bytes")
        if mode in {"info", "file"}:
            original = inspect_image(rp)
            if mode == "info":
                audit("view_image", f"{rp} mode=info")
                return [TextContent(type="text", text=json.dumps(
                    {"mode": mode, **original}, ensure_ascii=False))]
        preview = encode_preview(rp, max_width=max_width, format=format)
        if mode == "file":
            suffix = "jpg" if preview['format'] == "jpeg" else preview['format']
            destination = Path(WORKSPACE_DIR) / "artifacts" / "image-previews" / f"preview-{uuid.uuid4().hex}.{suffix}"
            saved = write_bytes(str(destination), preview['data'], SCENES_DIR,
                                WORKSPACE_DIR, {".jpg", ".png", ".webp"}, 300_000)
            summary = {"mode": mode, "original": original, "preview": {
                "path": saved['path'], "width": preview['width'], "height": preview['height'],
                "format": preview['format'], "bytes": saved['bytes'], "sha256": saved['sha256'],
            }}
            audit("view_image", f"{rp} mode=file preview={saved['path']}")
            return [TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))]
    except (ValueError, OSError) as e:
        return [TextContent(type="text", text=f"[rejected] {e}")]
    audit("view_image", f"{rp} preview={preview['width']}x{preview['height']} {preview['bytes']}B")
    return [Image(data=preview['data'], format=preview['format']),
            TextContent(type="text", text=f"preview {preview['width']}x{preview['height']} "
                        f"{preview['bytes']} bytes; original: {rp}")]


# --- direct command execution ---------------------------------------------
_JOBS: dict[str, dict] = {}


def _job_new(cmd: str, cwd: str, max_output_chars: int, timeout_seconds: int) -> str:
    jid = f"job-{uuid.uuid4().hex}"
    _JOBS[jid] = {"cmd": cmd, "cwd": cwd, "started": time.time(), "done": False,
                  "max_output_chars": max_output_chars, "timeout_seconds": timeout_seconds,
                  "log_path": str(EXEC_LOG_DIR / f"{jid}.log")}
    return jid


def _format_exec_result(jid: str, result: dict) -> str:
    return (f"[exit {result['code']}] job_id: {jid}\n"
            f"timed_out: {str(result['timed_out']).lower()}\n"
            f"log_path: {result['log_path']}\n{result['output']}")


async def _job_run(jid: str) -> dict:
    job = _JOBS[jid]
    try:
        result = await run_command(job['cmd'], job['cwd'], jid,
                                   max_output_chars=job['max_output_chars'],
                                   timeout_seconds=job['timeout_seconds'], log_dir=EXEC_LOG_DIR)
        job.update(result)
        audit("job_done", f"{jid} exit={result['code']} timed_out={result['timed_out']}")
    except Exception as e:
        result = {"code": -1, "output": f"[error] {e}", "log_path": job['log_path'], "timed_out": False}
        job.update(result)
    finally:
        job['done'] = True
    return result


EXEC_MAX_WAIT = 75  # stay under the client's request timeout (~100s for Notion)


def _log_tail(path: str, lines: int = 8, chars: int = 800) -> str:
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 8192))
            text = f.read().decode('utf-8', 'replace')
    except OSError:
        return ''
    text = '\n'.join(text.replace('\r', '\n').splitlines()[-lines:])
    return text[-chars:]


def _running_msg(jid: str) -> str:
    job = _JOBS[jid]
    tail = _log_tail(job['log_path'])
    return (f"running {jid} ({int(time.time() - job['started'])}s, still going; "
            f"call job_wait to keep waiting)\nlog_path: {job['log_path']}"
            + (f"\n--- log tail ---\n{tail}" if tail else ''))


async def _wait_job(jid: str, seconds: int) -> str:
    job = _JOBS[jid]
    task = job.get('task')
    if task is not None and not job['done']:
        try:
            # shield: if the client gives up, the command keeps running.
            await asyncio.wait_for(asyncio.shield(task), timeout=max(1, min(int(seconds), EXEC_MAX_WAIT)))
        except (asyncio.TimeoutError, TimeoutError):
            return _running_msg(jid)
        except asyncio.CancelledError:
            raise
    return _format_exec_result(jid, job)


@mcp.tool()
async def exec(cmd: str, cwd: str = "", background: bool = False,
               max_output_chars: int = 4000, timeout_seconds: int = 120,
               wait_seconds: int = 45) -> str:
    """Run shell directly. Full merged stdout/stderr is saved under the
    workspace at tasks/exec-logs/<job_id>.log. Oversize previews retain
    head+tail with a marker. max_output_chars defaults to 4000.
    timeout_seconds (default 120, max 86400) is the command's own limit,
    killing the whole process group and preserving partial output. The call
    returns after at most wait_seconds (default 45, max 75); a running job
    keeps running and returns its job ID + log tail. Continue with job_wait.
    background=true returns immediately. cwd defaults to jobs/; custom cwd
    must be under an allowed root (this is not an OS sandbox). A general-purpose
    Python with numpy/Pillow is usually at <workspace>/.venv/bin/python.
    ffmpeg on PATH supports drawtext."""
    try:
        cwd = resolve_allowed(cwd) if cwd else JOBS_DIR
        if not os.path.isdir(cwd):
            raise ValueError(f"not a directory: {cwd}")
        if not 1 <= max_output_chars <= 1000000 or not 1 <= timeout_seconds <= 86400:
            raise ValueError("max_output_chars must be1..1000000; timeout_seconds must be1..86400")
    except ValueError as e:
        return f"[rejected] {e}"
    jid = _job_new(cmd, cwd, max_output_chars, timeout_seconds)
    audit("exec", f"{jid} cwd={cwd} bg={background}")
    _JOBS[jid]['task'] = asyncio.create_task(_job_run(jid))
    if background:
        return f"started {jid} — poll with job_wait / job_status\nlog_path: {_JOBS[jid]['log_path']}"
    return await _wait_job(jid, wait_seconds)


@mcp.tool()
async def job_wait(job_id: str, seconds: int = 45) -> str:
    """Wait up to `seconds` (max 75) for an exec job. Returns the full exec result
    when done; otherwise running status + log tail (call again to keep waiting)."""
    if job_id not in _JOBS:
        return f"[error] unknown job: {job_id}"
    out = await _wait_job(job_id, seconds)
    return out if out.startswith('running') else 'done ' + out


@mcp.tool()
def job_status(job_id: str) -> str:
    """Check an exec job without waiting; returns result or running status + log tail."""
    job = _JOBS.get(job_id)
    if not job:
        return f"[error] unknown job: {job_id}"
    if not job['done']:
        return _running_msg(job_id)
    return f"done " + _format_exec_result(job_id, job)


def _public_url(url: str) -> tuple[bool, str]:
    """Allow HTTPS public URLs only; reject localhost/private/link-local targets."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False, "only https URLs are allowed"
        host = parsed.hostname
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return False, "private/link-local IP is blocked"
        except ValueError:
            # Resolve host and reject if any A/AAAA points to a non-public IP.
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
                addr = item[4][0]
                if not ipaddress.ip_address(addr).is_global:
                    return False, "URL resolves to a private/link-local IP"
        return True, ""
    except Exception as e:
        return False, f"invalid URL: {e}"


@mcp.tool()
async def fetch(url: str, path: str) -> str:
    """Download a public HTTPS URL into an allowed root. path is RELATIVE to an
    allowed root (e.g. 'claude/notion-bridge/renderer/scenes/assets/font.ttf').
    HTTPS only; private/link-local/localhost targets and oversized responses are
    rejected. Use for fonts, images, audio, etc."""
    ok, reason = _public_url(url)
    if not ok:
        return f"[rejected] {reason}"
    target = None
    for root in ALLOWED_ROOTS:
        cand = os.path.realpath(os.path.join(root, path))
        if cand == root or cand.startswith(root + os.sep):
            if not _is_sensitive(cand):
                target = cand
                break
    if target is None:
        return f"[rejected] path not under any allowed root or is sensitive: {path}"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    audit("fetch", f"{url} -> {target}")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as c:
            r = await c.get(url)
            if r.is_redirect:
                location = r.headers.get("location", "")
                ok, reason = _public_url(location)
                if not ok:
                    return f"[rejected] redirect: {reason}"
                r = await c.get(location)
            r.raise_for_status()
            if len(r.content) > _FETCH_MAX_BYTES:
                return f"[rejected] response too large: {len(r.content)} > {_FETCH_MAX_BYTES} bytes"
            with open(target, "wb") as f:
                f.write(r.content)
    except Exception as e:
        return f"[error] fetch failed: {e}"
    return f"downloaded {len(r.content)} bytes -> {target}"


@mcp.tool()
def write_binary(path: str, base64_data: str) -> str:
    """Write base64 content to scenes/ (legacy relative paths) or an absolute/~
    path under notion-workspace. Supports materials, archives tgz/tar/gz/zip/pdf
    and script/config extensions. Rejects '..' and symlink traversal; returns bytes
    and SHA-256. Existing files are replaced atomically after validation."""
    try:
        data = base64.b64decode(base64_data, validate=True)
        result = write_bytes(path, data, SCENES_DIR, WORKSPACE_DIR, _BINARY_EXTS, MAX_BINARY_BYTES)
    except (ValueError, OSError) as e:
        return f"[rejected] {e}"
    audit("write_binary", f"{result['path']} {result['bytes']}B")
    return f"wrote {result['path']} ({result['bytes']} bytes)\nsha256: {result['sha256']}"


def _resolve_scene(path: str) -> str:
    """Confine text scene writes to the scenes subtree."""
    rp = os.path.realpath(os.path.join(SCENES_DIR, path))
    if rp != SCENES_DIR and not rp.startswith(SCENES_DIR + os.sep):
        raise ValueError(f"path escapes scenes sandbox: {path}")
    if os.path.splitext(rp)[1].lower() not in _WRITE_EXTS:
        raise ValueError(f"extension not allowed: {path}")
    return rp


def _resolve_binary(path: str) -> str:
    """Confine binary/material writes to scenes and use a separate extension list."""
    rp = os.path.realpath(os.path.join(SCENES_DIR, path))
    if rp != SCENES_DIR and not rp.startswith(SCENES_DIR + os.sep):
        raise ValueError(f"path escapes scenes sandbox: {path}")
    if os.path.splitext(rp)[1].lower() not in _BINARY_EXTS:
        raise ValueError(f"binary extension not allowed: {path}")
    return rp


@mcp.tool()
def fs_write(path: str, content: str) -> str:
    """Write UTF-8 text to scenes/ (legacy relative paths) or absolute/~ paths
    inside notion-workspace. Supports prior text types plus py/sh/yaml/yml/toml/csv.
    Rejects '..' and symlinks; returns written UTF-8 byte count and SHA-256."""
    try:
        result = write_bytes(path, content.encode('utf-8'), SCENES_DIR, WORKSPACE_DIR, _WRITE_EXTS, MAX_WRITE_BYTES)
    except (ValueError, OSError) as e:
        return f"[rejected] {e}"
    audit("fs_write", f"{result['path']} {result['bytes']}B")
    return f"wrote {result['path']} ({result['bytes']} bytes)\nsha256: {result['sha256']}"


@mcp.tool()
async def render(scene: str, frames: int = 1, fps: int = 24,
                 size: str = "1280x720", name: str = "") -> list:
    """Render a three.js/WebGL scene in headless Chrome. scene is a file under
    scenes/ (e.g. 'my.html'). frames=1 returns a PNG preview inline; frames>1
    renders a frame sweep and encodes renders/<name>.mp4 (returns the path).
    The scene MUST read time from the URL param `t` (0..1) and render
    deterministically for that t — animate with t, not wall-clock. Import three
    via an importmap: {"imports":{"three":"../node_modules/three/build/three.module.js"}}."""
    try:
        rp = _resolve_scene(scene)
    except ValueError as e:
        return [TextContent(type="text", text=f"[rejected] {e}")]
    if not os.path.isfile(rp):
        return [TextContent(type="text", text=f"scene not found: {scene}")]
    rel = os.path.relpath(rp, RENDERER_DIR)
    out_name = name or os.path.splitext(os.path.basename(scene))[0]
    # Reject path syntax before passing the name to node; basename alone would
    # silently turn '../outside' into 'outside' instead of rejecting it.
    if (out_name in {".", ".."} or ".." in out_name or "/" in out_name
            or "\\" in out_name or not out_name.strip()):
        return [TextContent(type="text", text=f"[rejected] bad name: {out_name}")]
    cmd = ["node", RENDER_SCRIPT, rel, "--frames", str(max(1, min(frames, 240))),
           "--fps", str(fps), "--size", size, "--name", out_name]
    audit("render", f"{rel} frames={frames} fps={fps} size={size} name={out_name}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=RENDERER_DIR,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return [TextContent(type="text",
                            text=f"[timeout] render exceeded {RENDER_TIMEOUT}s")]
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        return [TextContent(type="text",
                            text=f"[render failed exit {proc.returncode}]\n{text[-3000:]}")]
    if frames == 1:
        png = os.path.realpath(os.path.join(RENDERS_DIR, f"{out_name}.png"))
        if os.path.isfile(png):
            return [Image(path=png),
                    TextContent(type="text", text=f"rendered {png}")]
        return [TextContent(type="text", text=text[-2000:])]
    mp4 = os.path.realpath(os.path.join(RENDERS_DIR, f"{out_name}.mp4"))
    return [TextContent(type="text",
                        text=f"{text.strip()}\nvideo: {mp4} ({os.path.getsize(mp4) if os.path.isfile(mp4) else '?'} bytes)")]


# --------------------------------------------------------------------------
# bearer-auth ASGI wrapper + entrypoint
# --------------------------------------------------------------------------
class BearerAuth:
    """Bearer-token gate with two client-compat exemptions:
    - CORS preflight (OPTIONS) never carries auth -> let it through.
    - Sessions established with a valid token stay trusted by session id
      (some clients only send Authorization on the first request).
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token
        self.sessions: dict[str, float] = {}  # session-id -> last seen

    def _remember(self, sid: str) -> None:
        self.sessions[sid] = time.time()
        if len(self.sessions) > 1000:  # cap: drop oldest
            for k in sorted(self.sessions, key=self.sessions.get)[:200]:
                self.sessions.pop(k, None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        token_ok = bool(supplied) and hmac.compare_digest(supplied, self.token)
        sid = headers.get("mcp-session-id", "")
        method = scope.get("method", "?")
        known = bool(sid) and sid in self.sessions
        ok = token_ok or known or method == "OPTIONS"
        if known:
            self._remember(sid)
        audit("http", f"{method} {scope.get('path','?')} "
                      f"auth={'yes' if auth else 'no'} sid={'known' if known else (sid[:8] or '-')} "
                      f"-> {'pass' if ok else '401'}")
        if not ok:
            await send({"type": "http.response.start", "status": 401,
                        "headers": [
                            (b"content-type", b"text/plain"),
                            # MCP spec: a 401 must advertise how to authenticate
                            (b"www-authenticate",
                             b'Bearer realm="notion-bridge", error="invalid_token", '
                             b'error_description="missing or bad bearer token"'),
                        ]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return

        async def send_wrapper(message):
            # trust sessions the app just created (from a token_ok request)
            if message["type"] == "http.response.start" and token_ok:
                for k, v in message.get("headers", []):
                    if k.decode().lower() == "mcp-session-id":
                        self._remember(v.decode())
            await send(message)

        await self.app(scope, receive, send_wrapper)


def build_app(token: str, allowed_hosts: list[str] | None = None):
    """Return the bearer-auth-wrapped Starlette MCP app.

    allowed_hosts extends the DNS-rebinding Host allowlist (e.g. the live
    tunnel domain). When ALLOW_ANY_HOST is set, rebinding protection is off
    entirely — acceptable because the server binds to loopback and every
    request still needs the bearer token.
    """
    from mcp.server.streamable_http import TransportSecuritySettings

    hosts = list(EXTRA_ALLOWED_HOSTS) + list(allowed_hosts or [])
    if ALLOW_ANY_HOST:
        ts = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        ts = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", *hosts],
        )
    app = mcp.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1",
                                  transport_security=ts)
    # Add independently authenticated immediate RPC before the MCP token gate.
    from starlette.routing import Route
    from .direct import rpc
    app.routes.insert(0, Route('/bridge/rpc', rpc, methods=['POST']))
    sdk_app = app
    mcp_app = BearerAuth(sdk_app, token)

    async def routed(scope, receive, send):
        if scope.get('path', '').rstrip('/') == '/bridge/rpc':
            return await sdk_app(scope, receive, send)
        return await mcp_app(scope, receive, send)

    app = routed
    # Notion's MCP client runs in a browser -> cross-origin. The SDK ships no
    # CORS handling, so preflight (OPTIONS) and the Authorization /
    # mcp-session-id response headers must be exposed explicitly. The real gate
    # is still the bearer token, so allowing any origin does not weaken auth.
    from starlette.middleware.cors import CORSMiddleware

    app = CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id", "www-authenticate", "retry-after",
                        "last-event-id", "content-type"],
        max_age=86400,
    )
    return app


def main() -> None:
    import uvicorn

    app = build_app(_TOKEN)
    print(f"notion-bridge listening on http://127.0.0.1:{PORT}/mcp "
          f"(roots={ALLOWED_ROOTS}, model={DELEGATE_MODEL})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", timeout_keep_alive=60)


if __name__ == "__main__":
    main()
