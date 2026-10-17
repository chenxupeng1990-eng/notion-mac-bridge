"""Stdlib-only execution tests; no server import or external service calls."""

from __future__ import annotations

import ast
import asyncio
import codecs
import importlib.util
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock


# Load only the helper by path, without importing the server/package entrypoint.
_HELPER_PATH = Path(__file__).resolve().parents[1] / "src" / "notion_bridge" / "execution.py"
_SPEC = importlib.util.spec_from_file_location("notion_bridge_execution", _HELPER_PATH)
execution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(execution)


def python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


class FormatOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.log_path = Path(self.temporary.name) / "output.log"

    def test_untruncated_text_is_exact_including_line_endings(self) -> None:
        for text in ("", "x", "abc", "abcde", "abcdef", "a\r\nb\rc\n"):
            with self.subTest(text=text):
                self.log_path.write_bytes(text.encode("utf-8"))
                self.assertEqual(execution.format_output(self.log_path, max(1, len(text))), text)

    def test_first_and_last_halves_with_exact_marker(self) -> None:
        text = "0123456789"
        self.log_path.write_bytes(text.encode())
        for cap in (1, 2, 5, 8):
            with self.subTest(cap=cap):
                head_count = (cap + 1) // 2
                tail_count = cap // 2
                marker = f"[... 截断 {len(text) - cap} 字符，完整输出见 {self.log_path} ...]"
                expected = text[:head_count] + marker + (text[-tail_count:] if tail_count else "")
                self.assertEqual(execution.format_output(self.log_path, cap), expected)

    def test_incremental_unicode_decode_across_chunk_boundary(self) -> None:
        text = "a" * (execution._READ_SIZE - 1) + "你🙂é́" * 10_000 + "終\r\n"
        raw = text.encode("utf-8")
        self.log_path.write_bytes(raw)
        marker = f"[... 截断 {len(text) - 17} 字符，完整输出见 {self.log_path} ...]"
        self.assertEqual(execution.format_output(self.log_path, 17), text[:9] + marker + text[-8:])
        self.assertEqual(execution.format_output(self.log_path, len(text)), text)
        self.assertEqual(self.log_path.read_bytes(), raw)

    def test_invalid_utf8_uses_replacement_without_changing_bytes(self) -> None:
        raw = b"before\xff\r\nafter\xe4\xbd"
        self.log_path.write_bytes(raw)
        self.assertEqual(execution.format_output(self.log_path, 100), raw.decode("utf-8", "replace"))
        self.assertEqual(self.log_path.read_bytes(), raw)

    def test_large_log_preview_uses_bounded_memory(self) -> None:
        chunk = b"0123456789abcdef" * 4096
        with self.log_path.open("wb") as log:
            for _ in range(128):
                log.write(chunk)
        tracemalloc.start()
        try:
            output = execution.format_output(self.log_path, 4000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        total = len(chunk) * 128
        marker = f"[... 截断 {total - 4000} 字符，完整输出见 {self.log_path} ...]"
        self.assertEqual(output, chunk[:2000].decode() + marker + chunk[-2000:].decode())
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_cap_validation(self) -> None:
        self.log_path.write_bytes(b"abc")
        for cap in (0, -1, 1_000_001, True, 1.5, "4000", None):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    execution.format_output(self.log_path, cap)
        self.assertEqual(execution.format_output(self.log_path, 1_000_000), "abc")


class RunCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.log_dir = self.root / "logs"

    async def run_command(self, cmd: str, job_id: str = "test-job", **kwargs) -> dict:
        return await execution.run_command(cmd, "", job_id, log_dir=self.log_dir, **kwargs)

    async def test_seq_preserves_every_line_and_default_preview_cap(self) -> None:
        result = await self.run_command("seq 1 200000")
        log_path = Path(result["log_path"])
        total_chars = 0
        with log_path.open("rb") as log:
            for number in range(1, 200_001):
                expected = f"{number}\n".encode()
                self.assertEqual(log.readline(), expected)
                total_chars += len(expected)
            self.assertEqual(log.read(1), b"")
        with log_path.open("rb") as log:
            first = log.read(2000).decode()
            log.seek(-2000, os.SEEK_END)
            last = log.read(2000).decode()
        marker = f"[... 截断 {total_chars - 4000} 字符，完整输出见 {log_path} ...]"
        self.assertEqual(result["output"], first + marker + last)
        self.assertEqual(result["total_chars"], total_chars)
        self.assertEqual(result["code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertIsInstance(result["duration_ms"], int)
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertTrue(log_path.is_absolute())
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log_dir.stat().st_mode), 0o700)

    async def test_merged_stdout_stderr_raw_bytes_and_exit_code(self) -> None:
        command = python_command(
            "import os; os.write(1, b'out\\r\\n'); os.write(2, b'err\\xff\\n'); "
            "os.write(1, b'final\\xe4\\xbd'); raise SystemExit(7)"
        )
        result = await self.run_command(command)
        expected = b"out\r\nerr\xff\nfinal\xe4\xbd"
        self.assertEqual(Path(result["log_path"]).read_bytes(), expected)
        self.assertEqual(result["output"], expected.decode("utf-8", "replace"))
        self.assertEqual(result["total_chars"], len(expected.decode("utf-8", "replace")))
        self.assertEqual(result["code"], 7)
        self.assertFalse(result["timed_out"])

    async def test_unicode_char_count_and_odd_preview_cap(self) -> None:
        text = "头🙂é\r\n" * 12_000 + "尾巴🙂"
        command = python_command(f"import os; os.write(1, ({text[:6]!r} * 12000 + '尾巴🙂').encode('utf-8'))")
        result = await self.run_command(command, max_output_chars=9)
        log_path = Path(result["log_path"])
        marker = f"[... 截断 {len(text) - 9} 字符，完整输出见 {log_path} ...]"
        self.assertEqual(result["output"], text[:5] + marker + text[-4:])
        self.assertEqual(result["total_chars"], len(text))
        self.assertEqual(log_path.read_bytes(), text.encode("utf-8"))

    async def test_empty_output(self) -> None:
        result = await self.run_command("true")
        self.assertEqual(result["output"], "")
        self.assertEqual(result["total_chars"], 0)
        self.assertEqual(result["code"], 0)
        self.assertEqual(Path(result["log_path"]).read_bytes(), b"")

    async def test_cwd_and_background_task_use_same_helper(self) -> None:
        directory = self.root / "working directory"
        directory.mkdir()
        task = asyncio.create_task(
            execution.run_command("pwd", str(directory), "background-job", log_dir=self.log_dir)
        )
        result = await task
        self.assertEqual(Path(result["output"].strip()).resolve(), directory.resolve())
        self.assertEqual(result["code"], 0)
        self.assertFalse(result["timed_out"])

    async def test_existing_log_and_symlink_are_not_overwritten(self) -> None:
        self.log_dir.mkdir()
        log_path = self.log_dir / "test-job.log"
        log_path.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            await self.run_command("printf overwritten")
        self.assertEqual(log_path.read_bytes(), b"original")
        target = self.root / "target"
        target.write_bytes(b"target-data")
        (self.log_dir / "symlink-job.log").symlink_to(target)
        with self.assertRaises(FileExistsError):
            await self.run_command("printf overwritten", "symlink-job")
        self.assertEqual(target.read_bytes(), b"target-data")

    async def test_validation_happens_before_log_creation_or_execution(self) -> None:
        for job_id in ("", "..", "a..b", "../escape", "a/b", "a\\b", "with space", "中文", "id\n", None):
            with self.subTest(job_id=job_id):
                with self.assertRaises(ValueError):
                    await self.run_command("true", job_id)
        for cap in (0, -1, 1_000_001, True, 1.5, "4000", None):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    await self.run_command("true", max_output_chars=cap)
        for timeout in (0, -1, 86_401, True, "120", None, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    await self.run_command("true", timeout_seconds=timeout)
        self.assertFalse(self.log_dir.exists())
        result = await self.run_command("printf ok", "abc_DEF-012", max_output_chars=1_000_000, timeout_seconds=86_400)
        self.assertEqual(result["output"], "ok")

    async def test_default_log_directory(self) -> None:
        self.assertEqual(
            execution.DEFAULT_LOG_DIR,
            Path(execution.os.environ.get("NOTION_WORKSPACE_DIR", str(Path.home() / "notion-workspace")))
            / "tasks" / "exec-logs",
        )
        with mock.patch.object(execution, "DEFAULT_LOG_DIR", self.log_dir):
            result = await execution.run_command("printf default", "", "default-job")
        self.assertEqual(Path(result["log_path"]), self.log_dir.resolve() / "default-job.log")
        self.assertEqual(result["output"], "default")

    def descendant_command(self, pid_path: Path) -> str:
        # Both the parent and its child ignore TERM. Group cleanup must still
        # stop the child, even though killing only the shell would leave it alive.
        child_source = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('child-ready', flush=True); time.sleep(60)"
        source = (
            "import os, pathlib, signal, subprocess, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"child = subprocess.Popen([sys.executable, '-u', '-c', {child_source!r}]); "
            f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); "
            "print('initial-output', flush=True); time.sleep(60)"
        )
        return python_command(source)

    async def wait_for_output(self, log_path: Path, needle: bytes = b"child-ready") -> None:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if log_path.exists() and needle in log_path.read_bytes():
                return
            await asyncio.sleep(0.01)
        self.fail(f"Command did not write {needle!r} in time")

    async def assert_child_stopped(self, pid_path: Path) -> None:
        pid = int(pid_path.read_text())
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            # A killed orphan can briefly be a zombie awaiting the OS reaper.
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
            ).stdout.strip()
            if not state or state.startswith("Z"):
                return
            await asyncio.sleep(0.01)
        self.fail(f"Descendant {pid} is still running")

    def emergency_cleanup(self, pid_path: Path) -> None:
        # Avoid leaking a child if a regression causes a lifecycle test to fail.
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def test_timeout_preserves_partial_output_kills_descendants_and_reaps(self) -> None:
        pid_path = self.root / "child.pid"
        self.addCleanup(self.emergency_cleanup, pid_path)
        original_spawn = asyncio.create_subprocess_shell
        processes = []

        async def capture_spawn(*args, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stderr"], asyncio.subprocess.STDOUT)
            self.assertNotEqual(kwargs["stdout"], asyncio.subprocess.PIPE)
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(execution.asyncio, "create_subprocess_shell", capture_spawn):
            result = await self.run_command(self.descendant_command(pid_path), timeout_seconds=0.75)
        self.assertTrue(result["timed_out"])
        self.assertLess(result["code"], 0)
        self.assertIn("initial-output\n", result["output"])
        self.assertIn("child-ready\n", result["output"])
        self.assertEqual(result["total_chars"], len(result["output"]))
        self.assertEqual(Path(result["log_path"]).read_text(), result["output"])
        await self.assert_child_stopped(pid_path)
        self.assertEqual(processes[0].returncode, result["code"])
        with self.assertRaises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    async def test_cancellation_preserves_log_kills_descendants_and_reaps(self) -> None:
        pid_path = self.root / "child.pid"
        self.addCleanup(self.emergency_cleanup, pid_path)
        original_spawn = asyncio.create_subprocess_shell
        processes = []

        async def capture_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(execution.asyncio, "create_subprocess_shell", capture_spawn):
            task = asyncio.create_task(self.run_command(self.descendant_command(pid_path)))
            try:
                await self.wait_for_output(self.log_dir / "test-job.log")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertIn(b"initial-output\n", (self.log_dir / "test-job.log").read_bytes())
        await self.assert_child_stopped(pid_path)
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    async def test_cancellation_during_spawn_and_repeated_cancellation(self) -> None:
        original_spawn = asyncio.create_subprocess_shell
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()
        processes = []

        async def delayed_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            spawn_started.set()
            await release_spawn.wait()
            return process

        with mock.patch.object(execution.asyncio, "create_subprocess_shell", delayed_spawn):
            task = asyncio.create_task(self.run_command(python_command("import time; print('started', flush=True); time.sleep(60)")))
            try:
                await asyncio.wait_for(spawn_started.wait(), timeout=5)
                await self.wait_for_output(self.log_dir / "test-job.log", b"started")
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            finally:
                release_spawn.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertIn(b"started\n", (self.log_dir / "test-job.log").read_bytes())
        self.assertIsNotNone(processes[0].returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    async def test_spawn_failure_preserves_exception_and_private_empty_log(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await execution.run_command("true", str(self.root / "missing-cwd"), "spawn-fail", log_dir=self.log_dir)
        log_path = self.log_dir / "spawn-fail.log"
        self.assertEqual(log_path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)



# Exercise the actual server job functions without its startup config, secrets,
# directory creation or network integrations. Only these AST nodes are loaded.
_SERVER_PATH = _HELPER_PATH.with_name("server.py")
_JOB_FUNCTIONS = {"_job_new", "_format_exec_result", "_job_run", "_log_tail",
                  "_running_msg", "_wait_job", "exec", "job_wait", "job_status"}
_job_tree = ast.parse(_SERVER_PATH.read_text())
_job_nodes = []
for _node in _job_tree.body:
    if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _node.name in _JOB_FUNCTIONS:
        _node.decorator_list = []
        _job_nodes.append(_node)
    elif isinstance(_node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "EXEC_MAX_WAIT" for target in _node.targets):
        _job_nodes.append(_node)
import types
import time
import uuid
jobs = types.ModuleType("server_jobs")
jobs.__dict__.update(asyncio=asyncio, time=time, uuid=uuid, os=os,
                     run_command=execution.run_command, audit=lambda *args: None)
exec(compile(ast.Module(body=_job_nodes, type_ignores=[]), str(_SERVER_PATH), "exec"), jobs.__dict__)


class JobWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        jobs._JOBS = {}
        jobs.JOBS_DIR = str(self.root)
        jobs.EXEC_LOG_DIR = self.root / "logs"
        jobs.resolve_allowed = lambda path: str(self.root) if path == str(self.root) else (_ for _ in ()).throw(ValueError("outside root"))

    async def asyncTearDown(self):
        tasks = [job['task'] for job in jobs._JOBS.values() if not job['task'].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def job_id(self):
        return next(iter(jobs._JOBS))

    async def test_foreground_completion_and_wait_done_result(self):
        reply = await jobs.exec("printf complete")
        jid = self.job_id()
        self.assertEqual(reply, jobs._format_exec_result(jid, jobs._JOBS[jid]))
        self.assertIn("timed_out: false", reply)
        self.assertTrue(reply.endswith("complete"))
        self.assertEqual(await jobs.job_wait(jid), "done " + reply)
        self.assertEqual(jobs.job_status(jid), "done " + reply)

    async def test_bounded_expiry_preserves_command_and_followup_completion(self):
        reply = await jobs.exec(python_command("import time; print('progress', flush=True); time.sleep(1.4); print('complete')"), wait_seconds=1)
        jid = self.job_id()
        self.assertTrue(reply.startswith("running " + jid))
        self.assertIn("call job_wait", reply)
        self.assertIn("--- log tail ---\nprogress", reply)
        self.assertFalse(jobs._JOBS[jid]['task'].done())
        self.assertFalse(jobs._JOBS[jid]['done'])
        done = await jobs.job_wait(jid, seconds=5)
        self.assertTrue(done.startswith("done [exit 0]"))
        self.assertTrue(done.endswith("progress\ncomplete\n"))
        self.assertFalse(jobs._JOBS[jid]['task'].cancelled())

    async def test_background_returns_immediately_without_waiting(self):
        with mock.patch.object(jobs, '_wait_job', side_effect=AssertionError("background must not wait")):
            reply = await jobs.exec("printf background", background=True)
        jid = self.job_id()
        self.assertTrue(reply.startswith("started " + jid))
        self.assertIn("job_wait / job_status", reply)
        self.assertIn(str(jobs.EXEC_LOG_DIR / f"{jid}.log"), reply)
        done = await jobs.job_wait(jid)
        self.assertTrue(done.endswith("background"))

    async def test_cancelled_request_leaves_job_running(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def command(cmd, cwd, jid, **kwargs):
            started.set()
            await release.wait()
            return dict(code=0, output="survived", log_path=jobs._JOBS[jid]['log_path'], timed_out=False)
        with mock.patch.object(jobs, 'run_command', command):
            request = asyncio.create_task(jobs.exec("held command"))
            await started.wait()
            jid = self.job_id()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertFalse(jobs._JOBS[jid]['task'].done())
            self.assertFalse(jobs._JOBS[jid]['done'])
            waiter = asyncio.create_task(jobs.job_wait(jid))
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertFalse(jobs._JOBS[jid]['task'].done())
            release.set()
            self.assertTrue((await jobs.job_wait(jid)).endswith("survived"))

    async def test_wait_caps_are_one_to_seventy_five_and_default_is_forty_five(self):
        reply = await jobs.exec("printf cap", background=True)
        jid = self.job_id()
        actual_wait_for = asyncio.wait_for
        captured = []
        async def timeout(future, *, timeout):
            captured.append(timeout)
            future.cancel()  # Cancel only the shield wrapper, never the command.
            raise asyncio.TimeoutError
        with mock.patch.object(jobs.asyncio, 'wait_for', timeout):
            for seconds in (-10, 0, 1, 45, 75, 76, 100000):
                self.assertTrue((await jobs.job_wait(jid, seconds)).startswith("running"))
            self.assertTrue((await jobs.job_wait(jid)).startswith("running"))
        self.assertEqual(captured, [1, 1, 1, 45, 75, 75, 75, 45])
        self.assertEqual(jobs.EXEC_MAX_WAIT, 75)
        await actual_wait_for(jobs._JOBS[jid]['task'], timeout=5)
        self.assertTrue(jobs.job_status(jid).startswith("done [exit 0]"))

    async def test_unknown_job_and_existing_execution_safeguards(self):
        self.assertEqual(await jobs.job_wait("missing"), "[error] unknown job: missing")
        self.assertEqual(jobs.job_status("missing"), "[error] unknown job: missing")
        for params in ({"cwd": "outside"}, {"max_output_chars": 0},
                       {"max_output_chars": 1000001}, {"timeout_seconds": 0},
                       {"timeout_seconds": 86401}):
            with self.subTest(params=params):
                self.assertTrue((await jobs.exec("true", **params)).startswith("[rejected]"))
        self.assertEqual(jobs._JOBS, {})
        self.assertFalse(jobs.EXEC_LOG_DIR.exists())

    async def test_command_timeout_is_independent_of_request_wait(self):
        reply = await jobs.exec(python_command("import time; print('partial', flush=True); time.sleep(60)"), timeout_seconds=1, wait_seconds=5)
        self.assertIn("timed_out: true", reply)
        self.assertIn("partial", reply)
        self.assertTrue(jobs._JOBS[self.job_id()]['done'])


class LogTailTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tail.log"

    def test_missing_empty_unicode_invalid_bytes_and_carriage_returns(self):
        self.assertEqual(jobs._log_tail(str(self.path)), "")
        self.path.write_bytes(b"")
        self.assertEqual(jobs._log_tail(str(self.path)), "")
        self.path.write_bytes("old\rprogress\r汉字\n".encode() + b"bad\xff\n")
        self.assertEqual(jobs._log_tail(str(self.path), lines=3), "progress\n汉字\nbad�")

    def test_line_character_and_byte_bounds_preserve_source(self):
        raw = b"old-prefix" * 10000 + b"\n" + b"".join(f"line-{i}\n".encode() for i in range(20))
        self.path.write_bytes(raw)
        expected = "\n".join(f"line-{i}" for i in range(12, 20))
        self.assertEqual(jobs._log_tail(str(self.path)), expected)
        self.assertEqual(jobs._log_tail(str(self.path), chars=10), expected[-10:])
        self.path.write_bytes(b"x" * 10000)
        self.assertEqual(jobs._log_tail(str(self.path)), "x" * 800)
        self.assertEqual(jobs._log_tail(str(self.path), chars=10000), "x" * 8192)
        self.assertEqual(self.path.read_bytes(), b"x" * 10000)


if __name__ == "__main__":
    unittest.main()
