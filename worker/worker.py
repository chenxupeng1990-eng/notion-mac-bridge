#!/usr/bin/env python3
"""Resident Miaoda Mac worker, using only the Python standard library.

Run with python3 worker.py [--once] [--config /absolute/worker-config.json].
Configuration defaults to ../worker-config.json, then ../config.json; supported
keys are worker_id, base_url, default_workdir, binaries {kimi, claude}, and
models {kimi, claude}. Flat kimi_binary/claude_binary/kimi_model/claude_model
keys are also accepted. Claude's model is optional and its own gateway settings
remain authoritative. Only MIAODA_WORKER_TOKEN is loaded from ../.env, without
exporting that file into the agent environment.

The agents run with the user's filesystem permissions. cwd selects the starting
working directory and is NOT a sandbox. Kimi's installed CLI requires the prompt
as the argument to -p; it cannot read this prompt from stdin. Its configured auto
permission mode is used without --auto/--yolo, which conflict with -p.

State is private, fsynced, and retained to deduplicate task IDs. Pending results
must be delivered before another task is claimed. An interrupted started record
is recovered as a failure, never executed again. Result POSTs must be idempotent
on the server, since an acknowledgement can be lost after acceptance.
"""

import argparse
import errno
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
from pathlib import Path
import shlex
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request

# Placeholder example only — always set base_url in worker-config.json to your
# deployed Miaoda app, e.g. https://<your-app>.feishuapp.com/app/<app_XXXX>
DEFAULT_BASE_URL = "https://example.feishuapp.com/app/app_example"
DEFAULT_WORKDIR = str(Path.home() / "notion-workspace")
DEFAULT_KIMI_MODEL = "magpie/group/notion-cheap"
RESULT_LIMIT = 160_000  # UTF-8 bytes; retained for legacy persisted results.
RESULT_PREVIEW_CHARS = 8_000
ERROR_LIMIT = 8_000  # UTF-8 bytes; below the cloud's 10,000-character limit.
WORKER_ID_LIMIT = 100
OUTPUT_LIMIT = 10 * 1024 * 1024  # Combined stdout and stderr.
HTTP_LIMIT = 2 * 1024 * 1024
STATE_LIMIT = 1024 * 1024
MAX_TIMEOUT = 1800
HEARTBEAT_SECONDS = 15
HEARTBEAT_FAILURE_SECONDS = 45
IDLE_GAPS = (1, 5, 15, 45)
LOG = logging.getLogger("miaoda.worker")
CLAUDE_HOST_ENV = {
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_CODE_DESKTOP_SKILL_SWITCHES",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CLAUDECODE",
}
RESULT_FIELDS = {
    "worker_id", "task_id", "status", "result", "error", "exit_code", "duration_ms",
}
RESULT_OPTIONAL_FIELDS = {"result_path", "result_chars", "result_sha256"}


class WorkerError(Exception):
    """Messages are fixed safe descriptions, never raw OS or HTTP exceptions."""


class APIError(WorkerError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def interruptible_wait(seconds, event):
    if event is None:
        time.sleep(seconds)
        return False
    return event.wait(seconds)


def utf8_cap(text, limit):
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    marker = "\n[truncated]"
    return encoded[:limit - len(marker)].decode("utf-8", errors="ignore") + marker


class Redactor:
    def __init__(self, token, environ):
        names = {name for name in environ if any(part in name.upper() for part in
                 ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "CREDENTIAL"))}
        referenced = environ.get("CLAUDE_CODE_HOST_AUTH_ENV_VAR")
        if referenced:
            names.add(referenced)
        self.values = sorted({token, *(environ.get(name, "") for name in names)} - {""},
                             key=len, reverse=True)

    def clean(self, text, limit):
        for secret in self.values:
            text = text.replace(secret, "[REDACTED]")
        return utf8_cap(text, limit)


@dataclass(frozen=True)
class Config:
    worker_id: str = field(default_factory=socket.gethostname)
    base_url: str = DEFAULT_BASE_URL
    default_workdir: str = DEFAULT_WORKDIR
    binaries: dict = field(default_factory=lambda: {
        "kimi": "/opt/homebrew/bin/kimi", "claude": "/opt/homebrew/bin/claude",
    })
    models: dict = field(default_factory=lambda: {"kimi": DEFAULT_KIMI_MODEL})
    # When set (worker-config.json "bridge_url"), advertised to the cloud as-is;
    # required for named tunnels on your own domain. Otherwise discovered from
    # cloudflared's quick-tunnel metrics endpoint.
    bridge_url: str = ""
    # Hostname suffixes accepted from quick-tunnel discovery.
    bridge_host_suffixes: tuple = (".trycloudflare.com",)

    def __post_init__(self):
        if (not isinstance(self.worker_id, str) or not self.worker_id.strip()
                or len(self.worker_id) > WORKER_ID_LIMIT):
            raise WorkerError("Invalid worker identifier")

    @classmethod
    def load(cls, path, parent):
        if path is None:
            path = parent / "worker-config.json"
            if not path.exists():
                path = parent / "config.json"
            if not path.exists():
                return cls()
        try:
            data = read_json(Path(path), STATE_LIMIT)
        except (OSError, ValueError) as exc:
            raise WorkerError("Cannot read worker configuration") from exc
        if not isinstance(data, dict):
            raise WorkerError("Worker configuration must be an object")
        defaults = cls()
        binaries = dict(defaults.binaries)
        models = dict(defaults.models)
        for key, target in (("binaries", binaries), ("models", models)):
            supplied = data.get(key, {})
            if not isinstance(supplied, dict):
                raise WorkerError("Invalid engine configuration")
            supplied = dict(supplied)
            for engine in ("kimi", "claude"):
                flat_key = engine + ("_binary" if key == "binaries" else "_model")
                if engine not in supplied and flat_key in data:
                    supplied[engine] = data[flat_key]
                if engine in supplied:
                    value = supplied[engine]
                    if not isinstance(value, str) or not value.strip() or "\x00" in value:
                        raise WorkerError("Invalid engine configuration")
                    target[engine] = value
        worker_id = data.get("worker_id", defaults.worker_id)
        workdir = data.get("default_workdir", defaults.default_workdir)
        base_url = data.get("base_url", defaults.base_url)
        if not isinstance(workdir, str) or "\x00" in workdir or not Path(workdir).is_absolute():
            raise WorkerError("Default workdir must be absolute")
        validate_base_url(base_url)
        if any(not Path(binary).is_absolute() for binary in binaries.values()):
            raise WorkerError("Agent binary paths must be absolute")
        bridge_url = data.get("bridge_url", "")
        if bridge_url:
            if (not isinstance(bridge_url, str) or not bridge_url.startswith("https://")
                    or re.search(r"\s", bridge_url)):
                raise WorkerError("bridge_url must be an https URL without whitespace")
        supplied = data.get("bridge_host_suffixes", None)
        if supplied is None:
            suffixes = (".trycloudflare.com",)
        elif not isinstance(supplied, list) or not supplied or not all(
                isinstance(v, str) and v.startswith(".") and 1 < len(v) <= 254 for v in supplied):
            raise WorkerError('bridge_host_suffixes must be a list of dotted suffixes such as ".example.com"')
        else:
            suffixes = tuple(supplied)
        return cls(worker_id, base_url, workdir, binaries, models,
                   bridge_url, tuple(suffixes))


def read_worker_token(path, environ):
    """Called only at runtime. Do not parse or export any other dotenv setting."""
    token = environ.get("MIAODA_WORKER_TOKEN", "")
    if not token:
        try:
            with Path(path).open("r", encoding="utf-8") as stream:
                while True:
                    line = stream.readline(65537)
                    if not line:
                        break
                    if len(line) > 65536:
                        raise WorkerError("Worker token file has an oversized line")
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:].lstrip()
                    name, separator, value = line.partition("=")
                    if separator and name.strip() == "MIAODA_WORKER_TOKEN":
                        parts = shlex.split(value, comments=True, posix=True)
                        if len(parts) != 1:
                            raise WorkerError("Invalid worker token setting")
                        token = parts[0]
                        break
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkerError("Cannot load worker token") from exc
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise WorkerError("MIAODA_WORKER_TOKEN is missing or invalid")
    return token


def validate_base_url(url):
    if not isinstance(url, str):
        raise WorkerError("Invalid API base URL")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise WorkerError("Invalid API base URL")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise WorkerError("API base URL must use HTTPS")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError("API redirect refused")


class BridgeDiscovery:
    def __init__(self, opener=None, clock=time.monotonic, host_suffixes=(".trycloudflare.com",),
                 static_url=None):
        # static_url: preconfigured bridge endpoint (named tunnel). Discovery is skipped.
        self.static_url = static_url
        self.host_suffixes = tuple(host_suffixes)
        self.opener = opener
        self.clock = clock
        self.expires_at = 0
        self.url = None
        self.lock = threading.Lock()

    def __call__(self):
        if self.static_url:
            return self.static_url
        with self.lock:
            now = self.clock()
            if now < self.expires_at:
                return self.url
            self.expires_at = now + 60
            self.url = None
            # Bypass host proxy settings for this local-only, unauthenticated probe.
            if self.opener is None:
                self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            try:
                with self.opener.open("http://127.0.0.1:20241/quicktunnel", timeout=1) as response:
                    raw = response.read(4097)
                if len(raw) > 4096:
                    return None
                data = json.loads(raw)
                host = data.get("hostname") if isinstance(data, dict) else None
                if not isinstance(host, str) or not host or len(host) > 300:
                    return None
                candidate = host if "://" in host else "https://" + host
                parsed = urllib.parse.urlsplit(candidate)
                hostname = parsed.hostname or ""
                labels = hostname.split(".")
                if (parsed.scheme != "https" or not any(hostname.endswith(sfx) for sfx in self.host_suffixes)
                        or parsed.username is not None or parsed.password is not None
                        or parsed.port is not None or parsed.path not in ("", "/")
                        or parsed.query or parsed.fragment or len(hostname) > 253
                        or any(not label or len(label) > 63 or label.startswith("-")
                               or label.endswith("-") or any(char not in
                               "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)
                               for label in labels)
                        or any(char.isspace() or ord(char) < 32 for char in host)):
                    return None
                self.url = "https://" + hostname + "/bridge/rpc"
            except urllib.error.HTTPError as exc:
                exc.close()
            except (OSError, urllib.error.URLError, http.client.HTTPException,
                    APIError, ValueError, UnicodeError):
                pass
            return self.url


class WorkerAPI:
    def __init__(self, base_url, token, opener=None, timeout=30, attempts=3,
                 wait=interruptible_wait, bridge_discovery=None):
        validate_base_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.timeout = timeout
        self.attempts = attempts
        self.wait = wait
        self.bridge_discovery = BridgeDiscovery() if bridge_discovery is None else bridge_discovery

    def _bridge_payload(self, payload):
        url = self.bridge_discovery()
        if url:
            payload["bridge_url"] = url
        return payload

    def post(self, endpoint, payload, stop=None, attempts=None):
        attempts = self.attempts if attempts is None else attempts
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(attempts):
            if stop is not None and stop.is_set():
                raise APIError("Request interrupted")
            request = urllib.request.Request(
                self.base_url + endpoint, data=body, method="POST",
                headers={"Authorization": "Bearer " + self.token,
                         "Content-Type": "application/json", "Accept": "application/json"},
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(HTTP_LIMIT + 1)
                if len(raw) > HTTP_LIMIT:
                    raise APIError("API response exceeded size limit")
                try:
                    reply = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise APIError("Invalid API JSON response") from exc
                if not isinstance(reply, dict):
                    raise APIError("Invalid API response")
                return reply
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()  # Do not read or log the body, headers, URL, or reason.
                failure = APIError("API HTTP request failed",
                                   code in (408, 425, 429) or 500 <= code < 600)
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                failure = APIError("API connection failed", retryable=True)
            except APIError as exc:
                failure = exc
            if not failure.retryable or attempt + 1 == attempts:
                raise failure
            if self.wait(min(8, 2 ** attempt), stop):
                raise APIError("Request interrupted")
        raise APIError("Request attempts exhausted")

    def claim(self, worker_id, stop):
        reply = self.post("/worker/claim", self._bridge_payload(
            {"worker_id": worker_id, "wait_seconds": 15}), stop)
        if "task" not in reply or (reply["task"] is not None and not isinstance(reply["task"], dict)):
            raise APIError("Invalid claim response")
        return reply["task"]

    def heartbeat(self, worker_id, task_id, stop):
        reply = self.post("/worker/heartbeat", self._bridge_payload(
            {"worker_id": worker_id, "task_id": task_id}), stop, attempts=1)
        status = reply.get("status")
        if status not in ("running", "cancelled", "failed", "succeeded"):
            raise APIError("Invalid heartbeat response")
        return status

    def report(self, payload, stop=None):
        reply = self.post("/worker/result", payload, stop)
        if reply.get("task_id") != payload["task_id"] or reply.get("status") not in (
                "succeeded", "failed", "cancelled"):
            raise APIError("Invalid result acknowledgement")


def private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise WorkerError("Invalid state directory")
    path.chmod(0o700)
    return path


def private_open(path, flags):
    fd = os.open(str(path), flags | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkerError("Invalid state file")
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "r+b" if flags & os.O_RDWR else "wb")
    except BaseException:
        os.close(fd)
        raise


def fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_result(workdir, text):
    """Write UTF-8 result.md via a pinned directory, never following symlinks."""
    workdir = Path(workdir)
    if not workdir.is_absolute() or ".." in workdir.parts:
        raise WorkerError("Invalid result directory")
    raw = text.encode("utf-8")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        for component in workdir.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child

        def check_destination():
            try:
                info = os.stat("result.md", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise WorkerError("Result destination must be a regular file")

        check_destination()
        name = ".result-" + os.urandom(16).hex() + ".tmp"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        temporary = name
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        check_destination()
        os.replace(temporary, "result.md", src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        try:
            os.fsync(directory)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        try:
            if temporary is not None:
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)
    return {"result_path": str(workdir / "result.md"), "result_chars": len(text),
            "result_sha256": hashlib.sha256(raw).hexdigest()}


def read_json(path, limit):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("JSON file exceeded limit")
    return json.loads(raw)


def validate_result(payload):
    if (not isinstance(payload, dict) or not RESULT_FIELDS <= set(payload)
            or set(payload) - RESULT_FIELDS - RESULT_OPTIONAL_FIELDS):
        raise WorkerError("Invalid persisted result")
    if (any(not isinstance(payload[key], str) or not payload[key] for key in ("worker_id", "task_id"))
            or not payload["worker_id"].strip() or len(payload["worker_id"]) > WORKER_ID_LIMIT):
        raise WorkerError("Invalid persisted result identity")
    if (payload["status"] not in ("succeeded", "failed")
            or not isinstance(payload["result"], str)
            or len(payload["result"].encode("utf-8")) > RESULT_LIMIT
            or (payload["error"] is not None and not isinstance(payload["error"], str))
            or type(payload["exit_code"]) is not int or type(payload["duration_ms"]) is not int
            or payload["duration_ms"] < 0):
        raise WorkerError("Invalid persisted result fields")
    if "result_path" in payload:
        path = payload["result_path"]
        if (not isinstance(path, str) or "\x00" in path or not Path(path).is_absolute()
                or ".." in Path(path).parts):
            raise WorkerError("Invalid persisted result path")
    if "result_chars" in payload and (type(payload["result_chars"]) is not int
                                      or payload["result_chars"] < 0):
        raise WorkerError("Invalid persisted result character count")
    if "result_sha256" in payload:
        digest = payload["result_sha256"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise WorkerError("Invalid persisted result digest")
    return payload


class StateStore:
    def __init__(self, root):
        self.root = private_dir(root)
        self.tasks = private_dir(self.root / "tasks")
        self.outbox = private_dir(self.root / "outbox")
        self.lock_file = None

    @staticmethod
    def key(task_id):
        return hashlib.sha256(task_id.encode("utf-8")).hexdigest()

    def task_dir(self, task_id):
        return self.tasks / self.key(task_id)

    def lock(self):
        stream = private_open(self.root / "worker.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise WorkerError("Another worker is using this state directory") from exc
        self.lock_file = stream

    def unlock(self):
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    def record(self, task_id):
        path = self.task_dir(task_id) / "record.json"
        if not path.exists():
            return None
        record = self._read(path)
        if record.get("task_id") != task_id or record.get("phase") not in ("started", "pending", "reported"):
            raise WorkerError("Invalid execution record")
        return record

    @staticmethod
    def _read(path):
        try:
            if path.is_symlink():
                raise WorkerError("Invalid state file")
            data = read_json(path, STATE_LIMIT)
        except (OSError, ValueError) as exc:
            raise WorkerError("Cannot read persisted state") from exc
        if not isinstance(data, dict):
            raise WorkerError("Invalid persisted state")
        return data

    def begin(self, worker_id, task_id):
        directory = private_dir(self.task_dir(task_id))
        atomic_json(directory / "record.json", {
            "worker_id": worker_id, "task_id": task_id, "phase": "started", "started_at": time.time(),
        })
        fsync_dir(self.tasks)
        return directory

    def enqueue(self, payload):
        validate_result(payload)
        directory = private_dir(self.task_dir(payload["task_id"]))
        # Outbox first: recovery must prefer a finished result over a started record.
        atomic_json(self.outbox / (self.key(payload["task_id"]) + ".json"), payload)
        atomic_json(directory / "record.json", {
            "worker_id": payload["worker_id"], "task_id": payload["task_id"],
            "phase": "pending", "payload": payload,
        })

    def acknowledged(self, payload):
        atomic_json(self.task_dir(payload["task_id"]) / "record.json", {
            "worker_id": payload["worker_id"], "task_id": payload["task_id"],
            "phase": "reported", "payload": payload,
        })
        self.discard_pending(payload["task_id"])

    def discard_pending(self, task_id):
        try:
            (self.outbox / (self.key(task_id) + ".json")).unlink()
        except FileNotFoundError:
            pass
        fsync_dir(self.outbox)

    def pending(self):
        for path in sorted(self.outbox.glob("*.json")):
            payload = validate_result(self._read(path))
            if path.stem != self.key(payload["task_id"]):
                raise WorkerError("Invalid outbox identity")
            yield payload

    def recover(self):
        for path in sorted(self.tasks.glob("*/record.json")):
            record = self._read(path)
            task_id = record.get("task_id")
            if not isinstance(task_id, str) or path.parent.name != self.key(task_id):
                raise WorkerError("Invalid execution record identity")
            pending = self.outbox / (self.key(task_id) + ".json")
            phase = record.get("phase")
            if phase == "reported":
                validate_result(record.get("payload"))
                self.discard_pending(task_id)
            elif phase == "pending":
                payload = validate_result(record.get("payload"))
                if not pending.exists():
                    self.enqueue(payload)
            elif phase == "started":
                if pending.exists():
                    # A crash after outbox fsync but before journal replacement.
                    payload = validate_result(self._read(pending))
                    if payload["task_id"] != task_id:
                        raise WorkerError("Invalid outbox identity")
                    self.enqueue(payload)
                else:
                    worker_id = record.get("worker_id")
                    started = record.get("started_at")
                    if not isinstance(worker_id, str) or type(started) not in (int, float):
                        raise WorkerError("Invalid interrupted execution record")
                    self.enqueue({
                        "worker_id": worker_id, "task_id": task_id, "status": "failed",
                        "result": "", "error": "Worker interrupted before recording a final result",
                        "exit_code": -1, "duration_ms": max(0, int((time.time() - started) * 1000)),
                    })
                    LOG.warning("interrupted_task_recovered")
            else:
                raise WorkerError("Invalid execution record phase")


@dataclass(frozen=True)
class Task:
    task_id: str
    prompt: str
    engine: str
    workdir: Path
    timeout_seconds: int

    @classmethod
    def parse(cls, data, config):
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 1024:
            raise WorkerError("Invalid task identifier")
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise WorkerError("Task prompt is empty or invalid")
        engine = data.get("engine", "kimi")
        if engine not in ("kimi", "claude"):
            raise WorkerError("Unsupported task engine")
        workdir = data.get("workdir")
        if workdir is None:
            root = Path(config.default_workdir).resolve(strict=True)
            if not root.is_dir():
                raise WorkerError("Default workspace is not a directory")
            tasks_root = root / "tasks"
            if tasks_root.is_symlink():
                raise WorkerError("Task workspace must not be a symlink")
            tasks_root.mkdir(mode=0o700, exist_ok=True)
            # Task IDs from the server are UUIDs; hash other identifiers so no
            # path supplied in a claim can escape the dedicated workspace.
            name = task_id if all(c in "0123456789abcdef-" for c in task_id) and len(task_id) == 36 else hashlib.sha256(task_id.encode()).hexdigest()
            task_dir = tasks_root / name
            if task_dir.is_symlink():
                raise WorkerError("Task workspace must not be a symlink")
            task_dir.mkdir(mode=0o700, exist_ok=True)
            workdir = str(task_dir)
        if not isinstance(workdir, str) or "\x00" in workdir or not Path(workdir).is_absolute():
            raise WorkerError("Task workdir must be an absolute directory")
        try:
            workdir = Path(workdir).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkerError("Task workdir does not exist") from exc
        if not workdir.is_dir():
            raise WorkerError("Task workdir is not a directory")
        timeout = data.get("timeout_seconds", MAX_TIMEOUT)
        if type(timeout) is not int or timeout <= 0:
            raise WorkerError("Task timeout must be a positive integer")
        return cls(task_id, prompt, engine, workdir, min(timeout, MAX_TIMEOUT))


@dataclass
class ExecutionResult:
    status: str
    result: str = ""
    error: Optional[str] = None
    exit_code: int = -1
    duration_ms: int = 0


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Mac process restrictions can deny the probe for an already empty
        # group. Treat it as unknown so we still attempt the final SIGKILL.
        return None


def terminate_group(process, grace=5):
    """Signal descendants even if the direct child already exited; always reap it."""
    pgid = process.pid  # start_new_session=True guarantees this identity.
    try:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            child_exited = process.poll() is not None
            exists = group_exists(pgid)
            if exists is False or (exists is None and child_exited):
                break
            time.sleep(0.05)
        if group_exists(pgid) is not False:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Never allow a failed group probe to skip direct-child reaping.
                if process.poll() is None:
                    process.kill()
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise WorkerError("Agent did not exit after process group cleanup") from exc


class ProcessExecutor:
    def __init__(self, config, api, redactor, stop, environ, heartbeat_seconds=HEARTBEAT_SECONDS,
                 poll_seconds=0.1, terminate_grace=5,
                 heartbeat_failure_seconds=HEARTBEAT_FAILURE_SECONDS):
        self.config = config
        self.api = api
        self.redactor = redactor
        self.stop = stop
        self.environ = dict(environ)
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.terminate_grace = terminate_grace
        self.heartbeat_failure_seconds = heartbeat_failure_seconds

    def command(self, task):
        if task.engine == "kimi":
            return [self.config.binaries["kimi"], "-p", task.prompt, "-m",
                    self.config.models.get("kimi", DEFAULT_KIMI_MODEL), "--output-format", "text"]
        command = [self.config.binaries["claude"], "-p", "--output-format", "json",
                   "--permission-mode", "bypassPermissions"]
        model = self.config.models.get("claude")
        if model:
            command.extend(["--model", model])
        return command

    def agent_env(self, engine):
        env = dict(self.environ)
        env.pop("MIAODA_WORKER_TOKEN", None)
        if engine == "claude":
            referenced = env.get("CLAUDE_CODE_HOST_AUTH_ENV_VAR")
            if referenced:
                env.pop(referenced, None)
            for name in CLAUDE_HOST_ENV:
                env.pop(name, None)
        return env

    def _heartbeat(self, task, done, cancelled, reason, health):
        failures = 0
        while not done.is_set() and not self.stop.is_set():
            try:
                status = self.api.heartbeat(self.config.worker_id, task.task_id, done)
                if done.is_set() or self.stop.is_set():
                    return
                if status != "running":
                    reason.append({"cancelled": "Task cancelled by server",
                                   "failed": "Task marked failed by server",
                                   "succeeded": "Task already completed by server"}[status])
                    cancelled.set()
                    return
                health[0] = time.monotonic()
                failures = 0
                gap = self.heartbeat_seconds
            except APIError:
                if done.is_set() or self.stop.is_set():
                    return
                LOG.warning("heartbeat_request_failed")
                failures = min(failures + 1, 6)
                gap = min(self.heartbeat_seconds, 2 ** (failures - 1))
            if done.wait(gap):
                return

    @staticmethod
    def _write_prompt(process, prompt):
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    @staticmethod
    def _size(paths):
        return sum(path.stat().st_size for path in paths)

    def execute(self, task, directory):
        started = time.monotonic()
        paths = (directory / "stdout.txt", directory / "stderr.txt")
        done, cancelled = threading.Event(), threading.Event()
        reason = []
        health = [started]  # Last confirmed running status; shared with heartbeat thread.
        process = None
        heartbeat = writer = None
        failure = None
        exit_code = -1
        try:
            if self.stop.is_set():
                failure = "Worker shutting down"
            else:
                with private_open(paths[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL) as stdout, \
                     private_open(paths[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL) as stderr:
                    process = subprocess.Popen(
                        self.command(task), cwd=str(task.workdir), env=self.agent_env(task.engine),
                        stdin=subprocess.PIPE if task.engine == "claude" else subprocess.DEVNULL,
                        stdout=stdout, stderr=stderr, start_new_session=True, shell=False,
                    )
                    if task.engine == "claude":
                        writer = threading.Thread(target=self._write_prompt,
                                                  args=(process, task.prompt), daemon=True)
                        writer.start()
                    heartbeat = threading.Thread(target=self._heartbeat,
                                                 args=(task, done, cancelled, reason, health), daemon=True)
                    heartbeat.start()
                    while True:
                        if self.stop.is_set():
                            failure = "Worker shutting down"
                        elif cancelled.is_set():
                            failure = reason[0]
                        elif time.monotonic() - started >= task.timeout_seconds:
                            failure = "Agent exceeded task timeout"
                        elif time.monotonic() - health[0] >= self.heartbeat_failure_seconds:
                            failure = "Worker lost task heartbeat for 45 seconds"
                        elif self._size(paths) > OUTPUT_LIMIT:
                            failure = "Agent output exceeded 10 MB limit"
                        if failure is not None or process.poll() is not None:
                            break
                        self.stop.wait(self.poll_seconds)
        except FileNotFoundError:
            failure = "Agent executable or working directory not found"
        except PermissionError:
            failure = "Agent could not start due to local permissions"
        except OSError as exc:
            failure = ("Prompt exceeds CLI argument capacity" if exc.errno == errno.E2BIG
                       else "Agent execution encountered a local IO error")
        finally:
            done.set()
            if process is not None:
                # Also collect any tool processes left behind after a normal exit.
                terminate_group(process, self.terminate_grace)
                exit_code = process.returncode
            if writer is not None:
                writer.join(timeout=1)
            if heartbeat is not None:
                # urllib cannot interrupt an in-flight request; this daemon stops on
                # return, while the supervising thread kills the agent immediately.
                heartbeat.join(timeout=0.1)
        if process is not None and failure is None and self.stop.is_set():
            failure = "Worker shutting down"
        if process is not None and failure is None and cancelled.is_set():
            failure = reason[0]
        output, stderr_text = "", ""
        remaining = OUTPUT_LIMIT
        for index, path in enumerate(paths):
            if not path.exists():
                continue
            with path.open("rb") as stream:
                raw = stream.read(remaining + 1)
            if len(raw) > remaining:
                failure = "Agent output exceeded 10 MB limit"
                raw = raw[:remaining]
            remaining -= len(raw)
            if index == 0:
                output = raw.decode("utf-8", errors="replace")
            else:
                stderr_text = raw.decode("utf-8", errors="replace")
        if task.engine == "claude" and output:
            try:
                message = json.loads(output)
                if isinstance(message, list):
                    message = next(item for item in reversed(message)
                                   if isinstance(item, dict) and item.get("type") == "result")
                if not isinstance(message, dict) or not isinstance(message.get("result", ""), str):
                    raise ValueError("Invalid Claude result")
                output = message.get("result", "")
                if message.get("is_error"):
                    errors = message.get("errors")
                    detail = "\n".join(errors) if isinstance(errors, list) and all(
                        isinstance(item, str) for item in errors) else output
                    failure = failure or (detail or "Claude returned is_error")
            except (ValueError, StopIteration):
                output = ""
                failure = failure or "Claude returned invalid result JSON"
        elif task.engine == "claude" and process is not None and failure is None:
            failure = "Claude returned no result JSON"
        if failure is None and exit_code != 0:
            failure = "Agent exited with a nonzero code"
            if stderr_text.strip():
                failure += "\n" + stderr_text
        return ExecutionResult(
            "failed" if failure else "succeeded", self.redactor.clean(output, OUTPUT_LIMIT),
            self.redactor.clean(failure, ERROR_LIMIT) if failure else None,
            exit_code, int((time.monotonic() - started) * 1000),
        )


class Worker:
    def __init__(self, config, api, state, executor, redactor, stop, wait=interruptible_wait):
        self.config, self.api, self.state = config, api, state
        self.executor, self.redactor, self.stop, self.wait = executor, redactor, stop, wait

    def drain_outbox(self, stop=None):
        for payload in self.state.pending():
            record = self.state.record(payload["task_id"])
            if record is not None and record["phase"] == "reported":
                self.state.discard_pending(payload["task_id"])
                continue
            # Apply current known secrets too (e.g. after credential rotation).
            payload["result"] = self.redactor.clean(payload["result"], RESULT_LIMIT)
            if payload["error"] is not None:
                payload["error"] = self.redactor.clean(payload["error"], ERROR_LIMIT)
            self.state.enqueue(payload)
            try:
                self.api.report(payload, stop)
            except APIError:
                LOG.warning("result_delivery_pending")
                return False
            self.state.acknowledged(payload)
            LOG.info("result_delivered")
        return True

    def handle(self, data):
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 1024:
            raise APIError("Claim has no valid task identifier")
        record = self.state.record(task_id)
        if record is not None:
            if record["phase"] == "started":
                self.state.recover()
            else:
                self.state.enqueue(validate_result(record.get("payload")))
            LOG.info("duplicate_task_execution_skipped")
            return
        directory = self.state.begin(self.config.worker_id, task_id)
        try:
            task = Task.parse(data, self.config)
            LOG.info("task_started")
            execution = self.executor.execute(task, directory)
        except WorkerError as exc:
            execution = ExecutionResult("failed", error=str(exc))
        except Exception:
            # Avoid tracebacks or arbitrary exception text containing argv/env.
            LOG.error("task_execution_failed")
            execution = ExecutionResult("failed", error="Unexpected local task execution failure")
        result = self.redactor.clean(execution.result, OUTPUT_LIMIT)
        payload = {
            "worker_id": self.config.worker_id, "task_id": task_id, "status": execution.status,
            "result": result,
            "error": self.redactor.clean(execution.error, ERROR_LIMIT) if execution.error else None,
            "exit_code": execution.exit_code, "duration_ms": execution.duration_ms,
        }
        if len(result) > RESULT_PREVIEW_CHARS:
            try:
                payload.update(atomic_result(task.workdir, result))
            except (OSError, WorkerError):
                # Preserve the full redacted output for manual recovery, but do
                # not advertise a private state path as a readable task artifact.
                try:
                    atomic_result(directory, result)
                except (OSError, WorkerError) as exc:
                    raise WorkerError("Cannot preserve full task result") from exc
                payload["status"] = "failed"
                payload["error"] = "Cannot safely save full result in task workspace"
                LOG.error("task_result_file_failed")
            payload["result"] = result[:RESULT_PREVIEW_CHARS]
        self.state.enqueue(payload)
        LOG.info("task_finished")

    def run(self, once=False):
        self.state.lock()
        try:
            LOG.info("worker_started; agents use full user permissions; cwd is not a sandbox")
            self.state.recover()
            had_pending = next(self.state.pending(), None) is not None
            idle, errors = 0, 0
            while not self.stop.is_set():
                if not self.drain_outbox(self.stop):
                    if once:
                        return 1
                    errors = min(errors + 1, 7)
                    self.wait(min(60, 2 ** (errors - 1)), self.stop)
                    continue
                if once and had_pending:
                    return 0  # Recovery-only invocation: do not claim another task.
                try:
                    data = self.api.claim(self.config.worker_id, self.stop)
                except APIError:
                    if self.stop.is_set():
                        break
                    LOG.warning("claim_request_failed")
                    if once:
                        return 1
                    errors = min(errors + 1, 7)
                    self.wait(min(60, 2 ** (errors - 1)), self.stop)
                    continue
                errors = 0
                if data is None:
                    if once:
                        return 0
                    self.wait(IDLE_GAPS[min(idle, len(IDLE_GAPS) - 1)], self.stop)
                    idle += 1
                    continue
                idle = 0
                self.handle(data)
                if self.stop.is_set():
                    # Payload is already durable. Shutdown never waits on the network.
                    break
                if once:
                    return 0 if self.drain_outbox(self.stop) else 1
                # The next iteration delivers the durable result, then immediately
                # claims again. Failed delivery uses the single backoff path above.
            return 0
        finally:
            self.state.unlock()
            LOG.info("worker_stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Claim at most one task, or drain recovered results")
    parser.add_argument("--config", type=Path, help="Optional worker JSON configuration")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = Path(__file__).resolve().parent
    parent = directory.parent
    stop = threading.Event()
    previous = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda signum, frame: stop.set())
        config = Config.load(args.config, parent)
        environ = dict(os.environ)
        token = read_worker_token(parent / ".env", environ)
        redactor = Redactor(token, environ)
        api = WorkerAPI(config.base_url, token,
                        bridge_discovery=BridgeDiscovery(
                            host_suffixes=config.bridge_host_suffixes,
                            static_url=config.bridge_url or None))
        state = StateStore(directory / "state")
        executor = ProcessExecutor(config, api, redactor, stop, environ)
        return Worker(config, api, state, executor, redactor, stop).run(args.once)
    except (WorkerError, OSError, ValueError):
        LOG.error("worker_stopped_due_to_configuration_or_state_error")
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
