"""Local stdlib tests. No real CLI/model calls, credentials, or remote requests.

Run: python3 -B -m unittest discover -s worker -v
All temporary files stay under this directory. The cancellation/timeout tests
start only short-lived fake Python agents and reap them before returning.
"""

import errno
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import worker

ROOT = Path(__file__).resolve().parent
DUMMY_TOKEN = "test-token-only-never-a-real-credential"


class FakeServer:
    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with owner.lock:
                    owner.requests.append((self.path, body, self.headers.get("Authorization")))
                code, reply = owner.responder(self.path, body)
                encoded = json.dumps(reply).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = "http://127.0.0.1:%s/app/app_test" % self.server.server_port

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def api(self, attempts=3):
        return worker.WorkerAPI(self.base_url, DUMMY_TOKEN, timeout=2, attempts=attempts,
                                wait=lambda seconds, stop: False, bridge_discovery=lambda: None)


class CountingExecutor:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or worker.ExecutionResult("succeeded", "local answer", exit_code=0,
                                                       duration_ms=123)

    def execute(self, task, directory):
        self.calls += 1
        return self.result


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workdir = self.root / "jobs"
        self.workdir.mkdir(mode=0o700)
        self.config = worker.Config(worker_id="test-mac", default_workdir=str(self.workdir))
        self.redactor = worker.Redactor(DUMMY_TOKEN, {})
        self.stop = threading.Event()

    def task(self, **changes):
        data = {"task_id": "task-123", "prompt": "test prompt", "engine": "kimi",
                "workdir": str(self.workdir), "timeout_seconds": 30}
        data.update(changes)
        return data

    def build(self, api, executor, wait=worker.interruptible_wait):
        state = worker.StateStore(self.root / "state")
        resident = worker.Worker(self.config, api, state, executor, self.redactor, self.stop, wait)
        return resident, state

    def executor(self, api, code, grace=0.2):
        executor = worker.ProcessExecutor(self.config, api, self.redactor, self.stop, {},
                                           heartbeat_seconds=0.05, poll_seconds=0.01,
                                           terminate_grace=grace)
        executor.command = lambda task: [sys.executable, "-I", "-c", code]
        return executor

    def test_failed_result_retry_restart_and_duplicate_claim_never_reexecute(self):
        report_fails = [True]
        claim_calls = []
        result_bodies = []

        def responder(path, payload):
            if path.endswith("/worker/claim"):
                claim_calls.append(payload)
                return 200, {"task": self.task()}
            if path.endswith("/worker/result"):
                result_bodies.append(payload)
                if report_fails[0]:
                    return 503, {"error": "temporary"}
                return 200, {"task_id": payload["task_id"], "status": payload["status"]}
            self.fail("Unexpected endpoint")

        executor = CountingExecutor(worker.ExecutionResult(
            "succeeded", "answer " + DUMMY_TOKEN, exit_code=0, duration_ms=123))
        with FakeServer(responder) as server:
            first, state = self.build(server.api(), executor)
            self.assertEqual(first.run(once=True), 1)
            self.assertEqual(executor.calls, 1)
            self.assertEqual(len(result_bodies), 3)  # Bounded HTTP retries.
            pending = list(state.pending())
            self.assertEqual(len(pending), 1)
            self.assertNotIn(DUMMY_TOKEN, json.dumps(pending))
            report_fails[0] = False
            recovered, _ = self.build(server.api(), executor)
            self.assertEqual(recovered.run(once=True), 0)
            self.assertEqual(len(claim_calls), 1)  # Outbox-only invocation.
            self.assertEqual(executor.calls, 1)
            self.assertEqual(list(state.pending()), [])
            # The server can repeat an already reported claim. Replay its result,
            # including after a restart, without repeating agent side effects.
            duplicate, _ = self.build(server.api(), executor)
            self.assertEqual(duplicate.run(once=True), 0)
            self.assertEqual(executor.calls, 1)
            self.assertEqual(result_bodies[-1]["result"], "answer [REDACTED]")
            self.assertEqual(claim_calls[0], {"worker_id": "test-mac", "wait_seconds": 15})
            self.assertEqual(set(result_bodies[-1]), worker.RESULT_FIELDS)
            self.assertTrue(all(auth == "Bearer " + DUMMY_TOKEN for _, _, auth in server.requests))
            self.assertTrue(all(path.startswith("/app/app_test/worker/")
                                for path, _, _ in server.requests))
            self.assertEqual(state.record("task-123")["phase"], "reported")
            for path in (state.root, state.tasks, state.outbox, state.task_dir("task-123")):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in state.root.rglob("*"):
                if path.is_file():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_interrupted_journal_is_reported_failed_without_execution(self):
        result_bodies = []

        def responder(path, payload):
            self.assertTrue(path.endswith("/worker/result"))
            result_bodies.append(payload)
            return 200, {"task_id": payload["task_id"], "status": payload["status"]}

        with FakeServer(responder) as server:
            executor = CountingExecutor()
            resident, state = self.build(server.api(), executor)
            state.begin(self.config.worker_id, "interrupted")
            self.assertEqual(resident.run(once=True), 0)
            self.assertEqual(executor.calls, 0)
            self.assertEqual(result_bodies[0]["status"], "failed")
            self.assertEqual(result_bodies[0]["exit_code"], -1)
            self.assertEqual(state.record("interrupted")["phase"], "reported")

    def test_recovery_prefers_finished_outbox_over_started_journal(self):
        executor = CountingExecutor()
        resident, state = self.build(mock.Mock(), executor)
        state.begin(self.config.worker_id, "finished")
        payload = {"worker_id": "test-mac", "task_id": "finished", "status": "succeeded",
                   "result": "completed before crash", "error": None, "exit_code": 0,
                   "duration_ms": 300}
        # Simulate the crash between durable outbox write and journal replacement.
        worker.atomic_json(state.outbox / (state.key("finished") + ".json"), payload)
        state.recover()
        self.assertEqual(list(state.pending()), [payload])
        self.assertEqual(state.record("finished")["payload"], payload)
        self.assertEqual(executor.calls, 0)

    def test_wrong_acknowledgement_keeps_outbox(self):
        def responder(path, payload):
            if path.endswith("/worker/claim"):
                return 200, {"task": self.task()}
            return 200, {"task_id": "another-task", "status": "succeeded"}

        with FakeServer(responder) as server:
            executor = CountingExecutor()
            resident, state = self.build(server.api(), executor)
            self.assertEqual(resident.run(once=True), 1)
            self.assertEqual(executor.calls, 1)
            self.assertEqual(len(list(state.pending())), 1)

    @unittest.skipUnless(hasattr(os, "fork"), "Mac/POSIX process group test")
    def test_cancellation_kills_child_group_and_reaps_direct_child(self):
        pidfile = self.root / "fake-pids.json"
        code = """
import json, os, signal, time
child = os.fork()
if child == 0:
    while True:
        time.sleep(0.1)
def terminate(signum, frame):
    os.waitpid(child, 0)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
with open(%r, 'w') as f:
    json.dump([os.getpid(), child], f)
while True:
    time.sleep(0.1)
""" % str(pidfile)

        def responder(path, payload):
            self.assertTrue(path.endswith("/worker/heartbeat"))
            deadline = time.monotonic() + 3
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            return 200, {"status": "cancelled"}

        with FakeServer(responder) as server:
            executor = self.executor(server.api(), code, grace=1)
            state = worker.StateStore(self.root / "state")
            directory = state.begin("test-mac", "cancelled")
            task = worker.Task.parse(self.task(task_id="cancelled"), self.config)
            started = time.monotonic()
            result = executor.execute(task, directory)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error, "Task cancelled by server")
            self.assertEqual(result.exit_code, 0)  # CLI zero exit must not override cancellation.
            pids = json.loads(pidfile.read_text())
            for pid in pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pids[0], os.WNOHANG)
            self.assertEqual(server.requests[0][1], {"worker_id": "test-mac", "task_id": "cancelled"})
            for path in (directory / "stdout.txt", directory / "stderr.txt"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_timeout_force_kills_uncooperative_agent(self):
        code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        api = mock.Mock()
        api.heartbeat.return_value = "running"
        executor = self.executor(api, code)
        state = worker.StateStore(self.root / "state")
        directory = state.begin("test-mac", "timeout")
        task = worker.Task.parse(self.task(task_id="timeout", timeout_seconds=1), self.config)
        result = executor.execute(task, directory)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "Agent exceeded task timeout")
        self.assertEqual(result.exit_code, -signal.SIGKILL)

    def test_shutdown_persists_failure_even_with_heartbeat_in_flight(self):
        api = mock.Mock()
        heartbeat_entered = threading.Event()
        release = threading.Event()

        def heartbeat(*args):
            heartbeat_entered.set()
            release.wait(3)
            return "running"

        api.heartbeat.side_effect = heartbeat
        code = "import time; time.sleep(30)"
        executor = self.executor(api, code)
        resident, state = self.build(api, executor)
        result = []

        def run_task():
            resident.handle(self.task(task_id="shutdown"))
            result.append(True)

        thread = threading.Thread(target=run_task)
        thread.start()
        try:
            self.assertTrue(heartbeat_entered.wait(3))
            self.stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            payload = list(state.pending())[0]
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error"], "Worker shutting down")
            self.assertEqual(payload["exit_code"], -signal.SIGTERM)
            api.report.assert_not_called()
        finally:
            self.stop.set()
            release.set()
            thread.join(timeout=3)

    def test_output_bomb_is_stopped_and_result_remains_bounded(self):
        code = "import os,time; os.write(1,b'x'*(11*1024*1024)); time.sleep(30)"
        api = mock.Mock()
        api.heartbeat.return_value = "running"
        executor = self.executor(api, code)
        state = worker.StateStore(self.root / "state")
        directory = state.begin("test-mac", "output")
        result = executor.execute(worker.Task.parse(self.task(task_id="output"), self.config), directory)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "Agent output exceeded 10 MB limit")
        self.assertLessEqual(len(result.result.encode("utf-8")), worker.OUTPUT_LIMIT)
        self.assertGreater(len(result.result), worker.RESULT_LIMIT)

    def test_claude_is_error_json_and_stdin_prompt(self):
        api = mock.Mock()
        api.heartbeat.return_value = "running"
        prompt = "prompt via stdin with 漢字"
        code = """
import json,sys
prompt = sys.stdin.read()
print(json.dumps({'type':'result','is_error':True,'result':'',
                  'errors':['failed: ' + prompt + ' ' + %r], 'session_id':'metadata'}))
""" % DUMMY_TOKEN
        executor = self.executor(api, code)
        state = worker.StateStore(self.root / "state")
        directory = state.begin("test-mac", "claude-error")
        task = worker.Task.parse(self.task(task_id="claude-error", engine="claude", prompt=prompt), self.config)
        result = executor.execute(task, directory)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 0)
        self.assertIn(prompt, result.error)
        self.assertNotIn(DUMMY_TOKEN, result.error)
        self.assertNotIn("session_id", result.result)
        self.assertEqual(result.result, "")

    def test_claude_success_preserves_full_parsed_result_before_report(self):
        api = mock.Mock()
        api.heartbeat.return_value = "running"
        code = "import json,sys; sys.stdin.read(); print(json.dumps({'result':'漢'*60000,'is_error':False,'api_key':'metadata'}))"
        executor = self.executor(api, code)
        state = worker.StateStore(self.root / "state")
        directory = state.begin("test-mac", "claude-ok")
        task = worker.Task.parse(self.task(task_id="claude-ok", engine="claude"), self.config)
        result = executor.execute(task, directory)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result, "漢" * 60000)
        self.assertNotIn("api_key", result.result)

    def test_large_unicode_results_store_full_text_and_readable_pages(self):
        full = "漢🙂" * 25000  # 50,000 characters, 175,000 UTF-8 bytes.
        self.assertEqual(len(full), 50000)
        self.assertGreater(len(full.encode("utf-8")), worker.RESULT_LIMIT)
        for engine in ("kimi", "claude"):
            for exit_code in (0, 1):
                with self.subTest(engine=engine, exit_code=exit_code):
                    api = mock.Mock()
                    api.heartbeat.return_value = "running"
                    code = "import json,sys; text='漢🙂'*25000; "
                    if engine == "claude":
                        code += "sys.stdin.read(); sys.stdout.write(json.dumps({'result':text,'metadata':'omit'})); "
                    else:
                        code += "sys.stdout.write(text); "
                    code += "sys.exit(%d)" % exit_code
                    resident, state = self.build(api, self.executor(api, code))
                    task_id = "large-%s-%d" % (engine, exit_code)
                    # Use the real task-specific default workspace, not the journal directory.
                    resident.handle(self.task(task_id=task_id, engine=engine, workdir=None))
                    payload = list(state.pending())[0]
                    artifact = Path(payload["result_path"])
                    expected_dir = worker.Task.parse(self.task(task_id=task_id, workdir=None),
                                                     self.config).workdir
                    self.assertEqual(artifact, expected_dir / "result.md")
                    self.assertTrue(artifact.is_absolute())
                    self.assertEqual(artifact.read_text(encoding="utf-8"), full)
                    self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
                    self.assertEqual(payload["result"], full[:8000])
                    self.assertEqual(payload["result_chars"], 50000)
                    self.assertEqual(payload["result_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
                    self.assertEqual(payload["status"], "succeeded" if exit_code == 0 else "failed")
                    self.assertEqual(payload["exit_code"], exit_code)
                    with artifact.open(encoding="utf-8") as stream:
                        pages = list(iter(lambda: stream.read(8000), ""))
                    self.assertEqual("".join(pages), full)
                    self.assertEqual(len(pages), 7)
                    self.assertTrue(resident.drain_outbox())
                    self.assertEqual(api.report.call_args.args[0], payload)
                    self.assertEqual(list(state.pending()), [])

    def test_preview_character_boundary_small_output_unchanged(self):
        for count in (7999, 8000, 8001):
            with self.subTest(count=count):
                text = "🙂" * count
                task_id = "boundary-%d" % count
                resident, state = self.build(mock.Mock(), CountingExecutor(
                    worker.ExecutionResult("succeeded", text, exit_code=0)))
                resident.handle(self.task(task_id=task_id, workdir=None))
                payload = state.record(task_id)["payload"]
                artifact = worker.Task.parse(self.task(task_id=task_id, workdir=None),
                                             self.config).workdir / "result.md"
                self.assertEqual(payload["result"], text[:8000])
                self.assertEqual(artifact.exists(), count > 8000)
                self.assertEqual(set(payload), worker.RESULT_FIELDS | (
                    worker.RESULT_OPTIONAL_FIELDS if count > 8000 else set()))

    def test_claude_is_error_with_large_result_still_persists_parsed_text(self):
        api = mock.Mock()
        api.heartbeat.return_value = "running"
        code = "import json,sys; sys.stdin.read(); print(json.dumps({'is_error':True, 'result':'漢'*9000, 'errors':['engine failed']}))"
        resident, state = self.build(api, self.executor(api, code))
        resident.handle(self.task(engine="claude"))
        payload = list(state.pending())[0]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["error"], "engine failed")
        self.assertEqual(payload["result"], "漢" * 8000)
        self.assertEqual(Path(payload["result_path"]).read_text(encoding="utf-8"), "漢" * 9000)

    def test_full_result_redacts_known_secret_beyond_old_byte_cap(self):
        api = mock.Mock()
        api.heartbeat.return_value = "running"
        code = "import sys; sys.stdout.write('x'*170000 + %r + '終')" % DUMMY_TOKEN
        resident, state = self.build(api, self.executor(api, code))
        resident.handle(self.task())
        payload = list(state.pending())[0]
        text = Path(payload["result_path"]).read_text(encoding="utf-8")
        self.assertEqual(text, "x" * 170000 + "[REDACTED]終")
        self.assertNotIn(DUMMY_TOKEN, text)
        self.assertNotIn(DUMMY_TOKEN, json.dumps(payload))
        self.assertEqual(payload["result_chars"], len(text))
        self.assertEqual(payload["result_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_large_result_retry_recovery_and_duplicate_do_not_execute_or_rewrite(self):
        api = mock.Mock()
        api.report.side_effect = worker.APIError("API connection failed", retryable=True)
        executor = CountingExecutor(worker.ExecutionResult("succeeded", "漢🙂" * 25000, exit_code=0))
        resident, state = self.build(api, executor)
        resident.handle(self.task())
        original = dict(list(state.pending())[0])
        self.assertFalse(resident.drain_outbox())
        self.assertEqual(executor.calls, 1)
        api.report.side_effect = None
        with mock.patch.object(worker, "atomic_result", side_effect=AssertionError("must not rewrite")):
            for _ in range(2):
                recovered, _ = self.build(api, executor)
                recovered.state.recover()
                recovered.state.recover()
                if not list(recovered.state.pending()):
                    recovered.handle(self.task())
                self.assertTrue(recovered.drain_outbox())
                self.assertEqual(api.report.call_args.args[0], original)
                self.assertEqual(executor.calls, 1)
        self.assertEqual(state.record("task-123")["payload"], original)

    def test_old_persisted_result_without_metadata_recovers_unchanged(self):
        old = {"worker_id": "test-mac", "task_id": "old-large", "status": "succeeded",
               "result": "漢" * 20000, "error": None, "exit_code": 0, "duration_ms": 10}
        api = mock.Mock()
        executor = CountingExecutor()
        resident, state = self.build(api, executor)
        state.enqueue(old)
        # A missing outbox is reconstructed from the legacy journal without output files.
        state.discard_pending(old["task_id"])
        state.recover()
        state.recover()
        self.assertEqual(list(state.pending()), [old])
        self.assertEqual(resident.run(once=True), 0)
        resident.handle(self.task(task_id=old["task_id"]))
        self.assertTrue(resident.drain_outbox())
        self.assertEqual(api.report.call_args.args[0], old)
        self.assertEqual(executor.calls, 0)
        self.assertFalse((self.workdir / "result.md").exists())

    def test_result_metadata_validation_allows_optional_fields_and_rejects_bad_values(self):
        base = {"worker_id": "test-mac", "task_id": "metadata", "status": "succeeded",
                "result": "preview", "error": None, "exit_code": 0, "duration_ms": 0}
        metadata = {"result_path": str(self.workdir / "result.md"), "result_chars": 50000,
                    "result_sha256": "a" * 64}
        for key, value in metadata.items():
            self.assertEqual(worker.validate_result(dict(base, **{key: value}))[key], value)
        self.assertEqual(worker.validate_result(dict(base, **metadata)), dict(base, **metadata))
        for key, bad_values in {
            "result_path": (None, 3, "relative/result.md", "/absolute/../result.md", "/bad\x00path"),
            "result_chars": (None, True, -1, 1.5, "50000"),
            "result_sha256": (None, 1, "a" * 63, "A" * 64, "z" * 64),
            "unexpected": ("extra",),
        }.items():
            for value in bad_values:
                with self.subTest(key=key, value=value), self.assertRaises(worker.WorkerError):
                    worker.validate_result(dict(base, **{key: value}))

    def test_atomic_result_replaces_regular_file_without_touching_hardlink(self):
        artifact = self.workdir / "result.md"
        original = self.root / "engine-artifact.md"
        original.write_text("engine output", encoding="utf-8")
        os.link(original, artifact)
        metadata = worker.atomic_result(self.workdir, "complete answer" * 1000)
        self.assertEqual(original.read_text(encoding="utf-8"), "engine output")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "complete answer" * 1000)
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        self.assertEqual(metadata["result_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(list(self.workdir.glob(".result-*.tmp")), [])

    def test_result_write_failure_preserves_full_private_output_and_reports_safe_error(self):
        full = "漢🙂" * 25000
        resident, state = self.build(mock.Mock(), CountingExecutor(
            worker.ExecutionResult("succeeded", full, exit_code=0)))
        original_replace = os.replace

        def fail_workspace_replace(source, dest, **kwargs):
            directory = kwargs.get("dst_dir_fd")
            if directory is not None and self.workdir.stat().st_ino == os.fstat(directory).st_ino:
                raise OSError(errno.EACCES, DUMMY_TOKEN)
            return original_replace(source, dest, **kwargs)

        with mock.patch.object(worker.os, "replace", side_effect=fail_workspace_replace):
            resident.handle(self.task())
        payload = list(state.pending())[0]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"], "Cannot safely save full result in task workspace")
        self.assertEqual(payload["result"], full[:8000])
        self.assertFalse(worker.RESULT_OPTIONAL_FIELDS & set(payload))
        self.assertEqual((state.task_dir("task-123") / "result.md").read_text(encoding="utf-8"), full)
        self.assertEqual(stat.S_IMODE((state.task_dir("task-123") / "result.md").stat().st_mode), 0o600)
        self.assertFalse((self.workdir / "result.md").exists())
        self.assertEqual(list(self.workdir.glob(".result-*.tmp")), [])
        self.assertNotIn(DUMMY_TOKEN, json.dumps(payload))

    def test_unsafe_result_destination_is_not_followed_and_marks_task_failed(self):
        full = "漢" * 9000
        target = self.root / "untouched.md"
        target.write_text("untouched", encoding="utf-8")
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind):
                artifact = self.workdir / "result.md"
                if kind == "symlink":
                    artifact.symlink_to(target)
                else:
                    artifact.mkdir()
                task_id = "unsafe-" + kind
                resident, state = self.build(mock.Mock(), CountingExecutor(
                    worker.ExecutionResult("succeeded", full, exit_code=0)))
                resident.handle(self.task(task_id=task_id))
                payload = state.record(task_id)["payload"]
                self.assertEqual(payload["status"], "failed")
                self.assertNotIn("result_path", payload)
                self.assertEqual(target.read_text(encoding="utf-8"), "untouched")
                self.assertEqual((state.task_dir(task_id) / "result.md").read_text(encoding="utf-8"), full)
                if kind == "symlink":
                    self.assertTrue(artifact.is_symlink())
                    artifact.unlink()
                else:
                    artifact.rmdir()

    def test_result_parent_symlink_after_execution_is_rejected(self):
        full = "漢" * 9000
        other = self.root / "other"
        other.mkdir()
        original = self.root / "original-jobs"

        class ReplacingExecutor:
            def execute(inner, task, directory):
                task.workdir.rename(original)
                task.workdir.symlink_to(other, target_is_directory=True)
                return worker.ExecutionResult("succeeded", full, exit_code=0)

        resident, state = self.build(mock.Mock(), ReplacingExecutor())
        resident.handle(self.task())
        payload = list(state.pending())[0]
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("result_path", payload)
        self.assertFalse((other / "result.md").exists())
        self.assertFalse((original / "result.md").exists())
        self.assertEqual((state.task_dir("task-123") / "result.md").read_text(encoding="utf-8"), full)

    def test_commands_and_claude_host_auth_isolation(self):
        environ = {name: "host-value" for name in worker.CLAUDE_HOST_ENV}
        environ.update({"CLAUDE_CODE_HOST_AUTH_ENV_VAR": "HOST_AUTH",
                        "HOST_AUTH": "dummy-host-secret", "MIAODA_WORKER_TOKEN": DUMMY_TOKEN,
                        "PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/test-home"})
        executor = worker.ProcessExecutor(self.config, mock.Mock(), self.redactor, self.stop, environ)
        kimi = worker.Task.parse(self.task(prompt="spaces 'quotes' $(never-run)"), self.config)
        command = executor.command(kimi)
        self.assertEqual(command, ["/opt/homebrew/bin/kimi", "-p", kimi.prompt, "-m",
                                   "magpie/group/notion-cheap", "--output-format", "text"])
        self.assertNotIn("--auto", command)
        self.assertNotIn("--yolo", command)
        claude = worker.Task.parse(self.task(engine="claude"), self.config)
        self.assertEqual(executor.command(claude), ["/opt/homebrew/bin/claude", "-p",
                         "--output-format", "json", "--permission-mode", "bypassPermissions"])
        clean = executor.agent_env("claude")
        self.assertFalse(set(worker.CLAUDE_HOST_ENV) & set(clean))
        self.assertNotIn("HOST_AUTH", clean)
        self.assertNotIn("MIAODA_WORKER_TOKEN", clean)
        self.assertEqual(clean["HOME"], "/test-home")
        self.assertEqual(clean["PATH"], environ["PATH"])
        self.assertEqual(executor.agent_env("kimi")["HOST_AUTH"], "dummy-host-secret")
        self.assertEqual(environ["MIAODA_WORKER_TOKEN"], DUMMY_TOKEN)

    def test_validation_reports_invalid_workdir_without_agent_call(self):
        executor = CountingExecutor()
        resident, state = self.build(mock.Mock(), executor)
        resident.handle(self.task(workdir="relative/path"))
        payload = list(state.pending())[0]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(executor.calls, 0)
        self.assertEqual(payload["error"], "Task workdir must be an absolute directory")
        self.assertEqual(worker.Task.parse(self.task(timeout_seconds=9999), self.config).timeout_seconds, 1800)
        with self.assertRaises(worker.WorkerError):
            worker.Task.parse(self.task(timeout_seconds=True), self.config)
        with self.assertRaises(worker.WorkerError):
            worker.Task.parse(self.task(workdir=str(self.root / "absent")), self.config)

    def test_idle_adaptive_gap_no_agent_and_immediate_claim_after_completion(self):
        api = mock.Mock()
        api.claim.side_effect = [self.task(), None, None, None, None]
        gaps = []

        def wait(seconds, stop):
            gaps.append(seconds)
            if len(gaps) == 4:
                stop.set()
            return stop.is_set()

        executor = CountingExecutor()
        resident, state = self.build(api, executor, wait)
        self.assertEqual(resident.run(), 0)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(api.claim.call_count, 5)
        self.assertEqual(gaps, [1, 5, 15, 45])
        api.heartbeat.assert_not_called()
        self.assertEqual(list(state.pending()), [])

    def test_error_backoff_is_capped_and_no_model_during_connection_errors(self):
        api = mock.Mock()
        api.claim.side_effect = worker.APIError("API connection failed", retryable=True)
        gaps = []

        def wait(seconds, stop):
            gaps.append(seconds)
            if len(gaps) == 9:
                stop.set()
            return stop.is_set()

        executor = CountingExecutor()
        resident, _ = self.build(api, executor, wait)
        self.assertEqual(resident.run(), 0)
        self.assertEqual(gaps, [1, 2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(executor.calls, 0)

    def test_empty_once_polls_exact_contract_and_does_not_run_agent(self):
        def responder(path, payload):
            return 200, {"task": None}

        with FakeServer(responder) as server:
            executor = CountingExecutor()
            resident, _ = self.build(server.api(), executor)
            self.assertEqual(resident.run(once=True), 0)
            self.assertEqual(executor.calls, 0)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0][1], {"worker_id": "test-mac", "wait_seconds": 15})

    def test_same_state_lock_refuses_second_worker(self):
        first = worker.StateStore(self.root / "state")
        second = worker.StateStore(self.root / "state")
        first.lock()
        self.addCleanup(first.unlock)
        with self.assertRaises(worker.WorkerError):
            second.lock()
        first.unlock()
        second.lock()
        second.unlock()

    def test_logs_never_include_prompt_token_or_result(self):
        api = mock.Mock()
        api.claim.return_value = self.task(prompt="private-prompt-sentinel")
        executor = CountingExecutor(worker.ExecutionResult("succeeded", "private-result-sentinel " + DUMMY_TOKEN,
                                                           exit_code=0))
        resident, _ = self.build(api, executor)
        with self.assertLogs(worker.LOG, level="INFO") as logs:
            self.assertEqual(resident.run(once=True), 0)
        text = "\n".join(logs.output)
        for sensitive in (DUMMY_TOKEN, "private-prompt-sentinel", "private-result-sentinel"):
            self.assertNotIn(sensitive, text)
        self.assertIn("cwd is not a sandbox", text)

    def test_configuration_fallback_hostname_and_override(self):
        # Configuration files here contain only nonsecret fixture settings.
        with mock.patch.object(worker.socket, "gethostname", return_value="unused-mock"):
            defaults = worker.Config.load(None, self.root)
        self.assertEqual(defaults.worker_id, socket.gethostname())
        (self.root / "config.json").write_text(json.dumps({"worker_id": "legacy-mac"}))
        self.assertEqual(worker.Config.load(None, self.root).worker_id, "legacy-mac")
        data = {"worker_id": "configured-mac", "base_url": "https://example.invalid/app/test",
                "default_workdir": str(self.workdir), "binaries": {"claude": "/test/claude"},
                "models": {"kimi": "custom/k3", "claude": "gateway-model"}}
        custom = self.root / "worker-config.json"
        custom.write_text(json.dumps(data))
        config = worker.Config.load(None, self.root)
        self.assertEqual(config.worker_id, "configured-mac")
        self.assertEqual(config.models["kimi"], "custom/k3")
        self.assertEqual(config.binaries["claude"], "/test/claude")
        self.assertEqual(worker.Config.load(custom, self.root), config)

    def test_default_tasks_get_separate_stable_workspaces(self):
        first = worker.Task.parse(self.task(task_id="first", workdir=None), self.config)
        second = worker.Task.parse(self.task(task_id="second", workdir=None), self.config)
        self.assertNotEqual(first.workdir, second.workdir)
        self.assertTrue(first.workdir.is_dir())
        self.assertEqual(first.workdir.parent, self.workdir / "tasks")
        self.assertEqual(worker.Task.parse(self.task(task_id="first", workdir=None), self.config).workdir,
                         first.workdir)
        explicit = worker.Task.parse(self.task(workdir=str(self.workdir)), self.config)
        self.assertEqual(explicit.workdir, self.workdir)

    def test_default_workspace_rejects_task_directory_symlink(self):
        (self.workdir / "tasks").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(worker.WorkerError):
            worker.Task.parse(self.task(workdir=None), self.config)

    def test_flat_configuration_keys_match_coordinator_config(self):
        custom = self.root / "flat-worker-config.json"
        custom.write_text(json.dumps({
            "worker_id": "demo-mac", "base_url": worker.DEFAULT_BASE_URL,
            "default_workdir": str(self.workdir), "kimi_model": worker.DEFAULT_KIMI_MODEL,
            "kimi_binary": "/opt/homebrew/bin/kimi", "claude_binary": "/test/claude",
        }))
        config = worker.Config.load(custom, self.root)
        self.assertEqual(config.models["kimi"], worker.DEFAULT_KIMI_MODEL)
        self.assertEqual(config.binaries["claude"], "/test/claude")
        self.assertEqual(config.worker_id, "demo-mac")
        self.assertNotIn("claude", config.models)

    def test_worker_id_cloud_length_boundary_applies_to_config_and_outbox(self):
        custom = self.root / "length-worker-config.json"
        for identifier in ("a" * 100, "漢" * 100):
            with self.subTest(identifier=identifier[:1]):
                custom.write_text(json.dumps({"worker_id": identifier}))
                self.assertEqual(worker.Config.load(custom, self.root).worker_id, identifier)
                self.assertEqual(worker.Config(worker_id=identifier).worker_id, identifier)
        custom.write_text(json.dumps({"worker_id": "a" * 101}))
        with self.assertRaises(worker.WorkerError):
            worker.Config.load(custom, self.root)
        with self.assertRaises(worker.WorkerError):
            worker.Config(worker_id="a" * 101)
        payload = {"worker_id": "a" * 101, "task_id": "overlong-id", "status": "failed",
                   "result": "", "error": "failure", "exit_code": -1, "duration_ms": 0}
        state = worker.StateStore(self.root / "state")
        with self.assertRaises(worker.WorkerError):
            state.enqueue(payload)
        self.assertEqual(list(state.pending()), [])

    def test_long_failure_reports_stay_within_cloud_error_constraint(self):
        for index, error in enumerate(("x" * 20_000, "漢" * 10_000)):
            with self.subTest(index=index):
                reports = []

                def responder(path, payload):
                    if path.endswith("/worker/claim"):
                        return 200, {"task": self.task(task_id="long-error-%s" % index)}
                    reports.append(payload)
                    return 200, {"task_id": payload["task_id"], "status": payload["status"]}

                with FakeServer(responder) as server:
                    executor = CountingExecutor(worker.ExecutionResult("failed", error=error, exit_code=1))
                    resident, state = self.build(server.api(), executor)
                    self.assertEqual(resident.run(once=True), 0)
                    self.assertEqual(executor.calls, 1)
                    sent = reports[0]["error"]
                    self.assertLessEqual(len(sent.encode("utf-8")), 8_000)
                    self.assertLess(len(sent), 10_000)
                    self.assertTrue(sent.endswith("[truncated]"))
                    self.assertNotIn("�", sent)
                    self.assertEqual(list(state.pending()), [])

    def test_old_outbox_error_is_recapped_before_recovery_upload(self):
        reports = []

        def responder(path, payload):
            self.assertTrue(path.endswith("/worker/result"))
            reports.append(payload)
            return 200, {"task_id": payload["task_id"], "status": payload["status"]}

        with FakeServer(responder) as server:
            executor = CountingExecutor()
            resident, state = self.build(server.api(), executor)
            state.enqueue({"worker_id": "test-mac", "task_id": "old-error", "status": "failed",
                           "result": "", "error": "x" * 16_000, "exit_code": 1, "duration_ms": 4})
            self.assertEqual(resident.run(once=True), 0)
            self.assertEqual(executor.calls, 0)
            self.assertLessEqual(len(reports[0]["error"].encode("utf-8")), 8_000)
            self.assertEqual(state.record("old-error")["payload"]["error"], reports[0]["error"])

    def test_bridge_discovery_caches_valid_tunnel_and_refreshes_after_60_seconds(self):
        opener = mock.Mock()
        opener.open.side_effect = [
            io.BytesIO(json.dumps({"hostname": "https://first.trycloudflare.com"}).encode()),
            io.BytesIO(json.dumps({"hostname": "second.trycloudflare.com"}).encode()),
        ]
        clock = mock.Mock(return_value=100)
        discovery = worker.BridgeDiscovery(opener=opener, clock=clock)
        self.assertEqual(discovery(), "https://first.trycloudflare.com/bridge/rpc")
        opener.open.assert_called_once_with("http://127.0.0.1:20241/quicktunnel", timeout=1)
        clock.return_value = 159.9
        self.assertEqual(discovery(), "https://first.trycloudflare.com/bridge/rpc")
        self.assertEqual(opener.open.call_count, 1)
        clock.return_value = 160
        self.assertEqual(discovery(), "https://second.trycloudflare.com/bridge/rpc")
        self.assertEqual(opener.open.call_count, 2)

    def test_bridge_discovery_omits_unavailable_invalid_or_oversized_tunnel(self):
        bad_hosts = (None, 42, "", "http://test.trycloudflare.com", "https://trycloudflare.com",
                     "https://trycloudflare.com.evil.test", "https://test.trycloudflare.com@evil.test",
                     "https://user@test.trycloudflare.com", "https://test.trycloudflare.com:443",
                     "https://test.trycloudflare.com/path", "https://test.trycloudflare.com?q=1",
                     "https://test.trycloudflare.com#fragment", "https://bad_host.trycloudflare.com",
                     "https://bad..trycloudflare.com", "https://-bad.trycloudflare.com",
                     "https://test.trycloudflare.com\n")
        for host in bad_hosts:
            with self.subTest(host=host):
                opener = mock.Mock()
                opener.open.return_value = io.BytesIO(json.dumps({"hostname": host}).encode())
                self.assertIsNone(worker.BridgeDiscovery(opener=opener)())
        for body in (b"invalid-json", b"{}", b"[]", b"x" * 4097):
            opener = mock.Mock()
            opener.open.return_value = io.BytesIO(body)
            self.assertIsNone(worker.BridgeDiscovery(opener=opener)())
        opener = mock.Mock()
        opener.open.side_effect = OSError("test failure")
        clock = mock.Mock(return_value=10)
        discovery = worker.BridgeDiscovery(opener=opener, clock=clock)
        self.assertIsNone(discovery())
        clock.return_value = 69
        self.assertIsNone(discovery())
        self.assertEqual(opener.open.call_count, 1)
        clock.return_value = 70
        self.assertIsNone(discovery())
        self.assertEqual(opener.open.call_count, 2)

    def test_bridge_url_published_only_in_claim_and_heartbeat_when_discovered(self):
        reports = []

        def responder(path, payload):
            reports.append((path, payload))
            if path.endswith("/worker/claim"):
                return 200, {"task": None}
            return 200, {"status": "running"}

        bridge = "https://test.trycloudflare.com/bridge/rpc"
        with FakeServer(responder) as server:
            api = worker.WorkerAPI(server.base_url, DUMMY_TOKEN, bridge_discovery=lambda: bridge)
            self.assertIsNone(api.claim("test-mac", self.stop))
            self.assertEqual(api.heartbeat("test-mac", "task-123", self.stop), "running")
            api.bridge_discovery = lambda: None
            self.assertIsNone(api.claim("test-mac", self.stop))
            self.assertEqual(api.heartbeat("test-mac", "task-123", self.stop), "running")
        self.assertEqual(reports[0][1], {"worker_id": "test-mac", "wait_seconds": 15,
                                        "bridge_url": bridge})
        self.assertEqual(reports[1][1], {"worker_id": "test-mac", "task_id": "task-123",
                                        "bridge_url": bridge})
        self.assertEqual(reports[2][1], {"worker_id": "test-mac", "wait_seconds": 15})
        self.assertEqual(reports[3][1], {"worker_id": "test-mac", "task_id": "task-123"})

    def test_authorization_error_has_no_http_retry(self):
        def responder(path, payload):
            return 401, {"error": "dummy-body-must-not-be-logged"}

        with FakeServer(responder) as server:
            api = server.api()
            with self.assertRaises(worker.APIError) as caught:
                api.claim("test-mac", self.stop)
            self.assertEqual(len(server.requests), 1)
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("dummy-body", str(caught.exception))

    def test_lost_acknowledgement_retries_same_result_without_model(self):
        accepted = []
        executor = CountingExecutor()
        api = mock.Mock()
        resident, state = self.build(api, executor)
        payload = {"worker_id": "test-mac", "task_id": "lost-ack", "status": "succeeded",
                   "result": "already accepted", "error": None, "exit_code": 0,
                   "duration_ms": 5}
        state.enqueue(payload)

        def accepted_but_lost(result, stop):
            accepted.append(dict(result))
            if len(accepted) == 1:
                raise worker.APIError("API connection failed", retryable=True)

        api.report.side_effect = accepted_but_lost
        self.assertEqual(resident.run(once=True), 1)
        self.assertEqual(len(list(state.pending())), 1)
        restarted, _ = self.build(api, executor)
        self.assertEqual(restarted.run(once=True), 0)
        self.assertEqual(accepted, [payload, payload])
        self.assertEqual(executor.calls, 0)
        api.claim.assert_not_called()

    def test_failed_delivery_uses_one_backoff_path_before_retries(self):
        api = mock.Mock()
        api.claim.return_value = self.task()
        api.report.side_effect = worker.APIError("API connection failed", retryable=True)
        gaps = []

        def wait(seconds, stop):
            gaps.append(seconds)
            stop.set()
            return True

        resident, state = self.build(api, CountingExecutor(), wait)
        self.assertEqual(resident.run(), 0)
        self.assertEqual(api.report.call_count, 1)
        self.assertEqual(gaps, [1])
        self.assertEqual(api.claim.call_count, 1)
        self.assertEqual(len(list(state.pending())), 1)

    def test_heartbeat_lease_watchdog_kills_agent_while_http_is_blocked(self):
        api = mock.Mock()
        entered, release = threading.Event(), threading.Event()

        def heartbeat(*args):
            entered.set()
            release.wait(3)
            raise worker.APIError("API connection failed", retryable=True)

        api.heartbeat.side_effect = heartbeat
        executor = self.executor(api, "import time; time.sleep(30)")
        executor.heartbeat_failure_seconds = 0.15
        state = worker.StateStore(self.root / "state")
        directory = state.begin("test-mac", "lost-heartbeat")
        task = worker.Task.parse(self.task(task_id="lost-heartbeat"), self.config)
        started = time.monotonic()
        try:
            result = executor.execute(task, directory)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error, "Worker lost task heartbeat for 45 seconds")
            self.assertEqual(result.exit_code, -signal.SIGTERM)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
