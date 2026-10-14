import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from starlette.requests import Request
from notion_bridge import direct
from notion_bridge.discovery import CachedMCPServer
from mcp.server.mcpserver import MCPServer


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_tools_cannot_be_mutated_by_caller(self):
        server = CachedMCPServer('test')
        @server.tool()
        def echo(value: str) -> str:
            return value
        first = await server.list_tools()
        first[0].description = 'mutated'
        second = await server.list_tools()
        self.assertNotEqual(second[0].description, 'mutated')

    async def test_retry_is_only_around_discovery(self):
        server = CachedMCPServer('test')
        original = MCPServer.list_tools
        count = 0
        async def flaky(instance):
            nonlocal count
            count += 1
            if count == 1: raise OSError('temporary discovery error')
            return await original(instance)
        with mock.patch.object(MCPServer,'list_tools',flaky):
            self.assertEqual(await server.list_tools(),[])
            self.assertEqual(await server.list_tools(),[])
        self.assertEqual(count,2)


class ReadFileTests(unittest.TestCase):
    def test_character_pages_reassemble_unicode_and_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();p=root/'result.md';content='汉😀é\n'*13000;p.write_text(content)
            with mock.patch.object(direct,'WORKSPACE',root):
                chunks=[];offset=0
                while True:
                    reply=direct.read_file(str(p),offset,3333)
                    chunks.append(reply['content']);offset=reply['next_offset']
                    self.assertEqual(reply['sha256'],hashlib.sha256(p.read_bytes()).hexdigest())
                    if reply['eof']:break
                self.assertEqual(''.join(chunks),content)
                self.assertEqual(reply['total_chars'],len(content))
    def test_traversal_secret_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();allowed=root/'allowed';allowed.mkdir();outside=root/'outside.txt';outside.write_text('secret')
            (allowed/'escape.txt').symlink_to(outside);(allowed/'.env').write_text('secret')
            with mock.patch.object(direct,'WORKSPACE',allowed):
                for p in ('../outside.txt',str(allowed/'escape.txt'),str(allowed/'.env')):
                    with self.assertRaises(ValueError):direct.read_file(p)


class SecureReadFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        patcher = mock.patch.object(direct, 'WORKSPACE', self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_relative_workspace_paths_and_actual_task_root(self):
        target = self.workspace / 'result.md'
        target.write_text('workspace result')
        self.assertEqual(direct.read_file('result.md')['content'], 'workspace result')
        task_root = self.base / 'actual-project'
        task_root.mkdir()
        artifact = task_root / 'result.md'
        artifact.write_text('actual task result')
        with self.assertRaises(ValueError):
            direct.read_file(str(artifact))
        self.assertEqual(direct.read_file(str(artifact), task_root=str(task_root))['content'],
                         'actual task result')
        # A task root supplements the normal roots without changing relative paths.
        self.assertEqual(direct.read_file('result.md', task_root=str(task_root))['content'],
                         'workspace result')
        sibling = self.base / 'actual-project-other'
        sibling.mkdir()
        (sibling / 'result.md').write_text('outside')
        with self.assertRaises(ValueError):
            direct.read_file(str(sibling / 'result.md'), task_root=str(task_root))

    def test_legacy_jobs_root_is_allowed_via_env_override(self):
        legacy = self.base / 'legacy-jobs'
        legacy.mkdir()
        artifact = legacy / 'read-test-result.md'
        artifact.write_text('legacy jobs result')
        # Realpath both sides: tempfile dirs are /var -> /private/var symlinks.
        artifact = artifact.resolve()
        with mock.patch.dict(os.environ, {'NOTION_BRIDGE_JOBS_DIR': str(legacy.resolve())}):
            reply = direct.read_file(str(artifact))
        self.assertEqual(reply['content'], 'legacy jobs result')
        self.assertEqual(reply['path'], str(artifact))

    def test_sensitive_spelling_is_rejected_before_resolve(self):
        safe = self.workspace / 'safe.txt'
        safe.write_text('safe')
        names = ('.env', '.env.production', '.ENV.py', 'audit.log', 'AUDIT.LOG',
                 '.git/safe.txt', '.venv/safe.txt', '.env-dir/safe.txt')
        for name in names:
            with self.subTest(name=name):
                alias = self.workspace / name
                alias.parent.mkdir(parents=True, exist_ok=True)
                alias.symlink_to(safe)
                with mock.patch.object(Path, 'resolve', side_effect=AssertionError('must reject first')):
                    with self.assertRaisesRegex(ValueError, 'sensitive'):
                        direct.read_file(str(alias))
                alias.unlink()

    def test_resolved_sensitive_target_is_rejected(self):
        secret = self.workspace / '.env.production'
        secret.write_text('never read')
        (self.workspace / 'normal.txt').symlink_to(secret)
        with self.assertRaisesRegex(ValueError, 'sensitive'):
            direct.read_file('normal.txt')

    def test_dotdot_and_invalid_paths_rejected_before_normalization(self):
        for path in ('inside/../result.md', '~/../etc/result.md', '../outside.md',
                     '', '\0result.md', None, b'result.md'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    direct.read_file(path)

    def test_task_root_must_be_existing_absolute_nonsensitive_directory(self):
        target = self.workspace / 'result.md'
        target.write_text('safe')
        directory = self.base / 'actual-project'
        directory.mkdir()
        alias = self.base / 'alias'
        alias.symlink_to(directory, target_is_directory=True)
        invalid = ('relative', str(directory / '..' / directory.name),
                   str(alias), str(target), str(self.base / '.env-directory'), '', b'bytes')
        for root in invalid:
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    direct.read_file(str(target), task_root=root)
        with self.assertRaises(FileNotFoundError):
            direct.read_file(str(target), task_root=str(self.base / 'absent'))

    def test_valid_tilde_task_root(self):
        directory = self.base / 'actual-project'
        directory.mkdir()
        target = directory / 'result.md'
        target.write_text('task result')
        with mock.patch.dict(os.environ, {'HOME': str(self.base)}):
            reply = direct.read_file('~/actual-project/result.md', task_root='~/actual-project')
        self.assertEqual(reply['content'], 'task result')

    def test_all_in_root_symlinks_and_nonregular_files_rejected(self):
        source = self.workspace / 'source.md'
        source.write_text('safe')
        link = self.workspace / 'link.md'
        link.symlink_to(source)
        directory = self.workspace / 'directory'
        directory.mkdir()
        (directory / 'result.md').write_text('safe')
        directory_link = self.workspace / 'directory-link'
        directory_link.symlink_to(directory, target_is_directory=True)
        for path in (link, directory_link / 'result.md'):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    direct.read_file(str(path))
        with self.assertRaisesRegex(ValueError, 'regular file'):
            direct.read_file(str(directory))
        fifo = self.workspace / 'pipe'
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            direct.read_file(str(fifo))

    def test_file_swapped_to_symlink_after_resolve_is_rejected(self):
        target = self.workspace / 'result.md'
        target.write_text('safe')
        secret = self.base / 'outside.md'
        secret.write_text('secret')
        real_resolve = Path.resolve
        changed = False

        def resolve(path, *args, **kwargs):
            nonlocal changed
            resolved = real_resolve(path, *args, **kwargs)
            if path == target and not changed:
                changed = True
                target.unlink()
                target.symlink_to(secret)
            return resolved

        with mock.patch.object(Path, 'resolve', resolve):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                direct.read_file(str(target))
        self.assertTrue(changed)
        self.assertEqual(secret.read_text(), 'secret')

    def test_directory_swapped_to_symlink_during_traversal_is_rejected(self):
        parent = self.workspace / 'parent'
        parent.mkdir()
        (parent / 'result.md').write_text('safe')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'result.md').write_text('secret')
        real_open = os.open
        changed = False

        def opened(name, flags, *args, **kwargs):
            nonlocal changed
            if name == 'parent' and kwargs.get('dir_fd') is not None and not changed:
                changed = True
                parent.rename(self.workspace / 'original-parent')
                parent.symlink_to(outside, target_is_directory=True)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(direct.os, 'open', opened):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                direct.read_file(str(parent / 'result.md'))
        self.assertTrue(changed)
        self.assertEqual((outside / 'result.md').read_text(), 'secret')

    def test_open_parent_descriptor_survives_later_path_symlink_swap(self):
        parent = self.workspace / 'parent'
        parent.mkdir()
        (parent / 'result.md').write_text('safe result')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'result.md').write_text('secret')
        real_open = os.open
        changed = False

        def opened(name, flags, *args, **kwargs):
            nonlocal changed
            if name == 'result.md' and kwargs.get('dir_fd') is not None and not changed:
                changed = True
                parent.rename(self.workspace / 'original-parent')
                parent.symlink_to(outside, target_is_directory=True)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(direct.os, 'open', opened):
            reply = direct.read_file(str(parent / 'result.md'))
        self.assertTrue(changed)
        self.assertEqual(reply['content'], 'safe result')
        self.assertEqual(reply['sha256'], hashlib.sha256(b'safe result').hexdigest())

    def test_task_root_symlink_swap_is_rejected(self):
        directory = self.base / 'task'
        directory.mkdir()
        target = directory / 'result.md'
        target.write_text('safe')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'result.md').write_text('secret')
        real_resolve = Path.resolve
        changed = False

        def resolve(path, *args, **kwargs):
            nonlocal changed
            resolved = real_resolve(path, *args, **kwargs)
            if path == target and not changed:
                changed = True
                directory.rename(self.base / 'original-task')
                directory.symlink_to(outside, target_is_directory=True)
            return resolved

        with mock.patch.object(Path, 'resolve', resolve):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                direct.read_file(str(target), task_root=str(directory))
        self.assertTrue(changed)

    def test_descriptors_are_closed_when_open_fails(self):
        parent = self.workspace / 'parent'
        parent.mkdir()
        (parent / 'result.md').write_text('safe')
        real_open = os.open
        opened_fds = []

        def opened(name, flags, *args, **kwargs):
            if name == 'result.md':
                raise PermissionError('simulated failure')
            descriptor = real_open(name, flags, *args, **kwargs)
            opened_fds.append(descriptor)
            return descriptor

        with mock.patch.object(direct.os, 'open', opened):
            with self.assertRaises(PermissionError):
                direct.read_file(str(parent / 'result.md'))
        for descriptor in opened_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_truncated_utf8_replacement_eof_and_limits(self):
        target = self.workspace / 'result.md'
        data = b'A\xe6\xb1\x89\xf0\x9f\x98\x80\xff\xe2'
        target.write_bytes(data)
        content = data.decode('utf-8', errors='replace')
        reply = direct.read_file(str(target), 1, 3)
        self.assertEqual(reply['content'], content[1:4])
        self.assertEqual(reply['total_chars'], len(content))
        self.assertEqual(reply['sha256'], hashlib.sha256(data).hexdigest())
        reply = direct.read_file(str(target), 100, 1)
        self.assertEqual(reply['content'], '')
        self.assertEqual(reply['next_offset'], 100)
        self.assertTrue(reply['eof'])
        for offset, limit in ((-1, 1), (True, 1), (0, 0), (0, 100001), (0, True)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaises(ValueError):
                    direct.read_file(str(target), offset, limit)


class DirectAuthTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def read_request(path, task_root, authorization=None):
        raw = json.dumps({'method': 'read_file', 'params': {
            'path': str(path), '_task_root': str(task_root), 'offset': 0, 'limit': 4000,
        }}).encode()
        used = False

        async def receive():
            nonlocal used
            if not used:
                used = True
                return {'type': 'http.request', 'body': raw, 'more_body': False}
            return {'type': 'http.disconnect'}

        headers = [(b'mcp-session-id', b'trusted')]
        if authorization is not None:
            headers.append((b'authorization', authorization))
        return Request({'type': 'http', 'method': 'POST', 'path': '/bridge/rpc',
                        'headers': headers, 'query_string': b''}, receive)

    async def test_internal_task_root_is_forwarded_after_worker_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / 'result.md'
            target.write_text('actual task result')
            request = self.read_request(target, root, b'Bearer independent-test-token')
            with mock.patch.object(direct, '_token', return_value='independent-test-token'):
                response = await direct.rpc(request)
            self.assertEqual(response.status_code, 200)
            result = json.loads(response.body)
            self.assertEqual(result['content'], 'actual task result')
            self.assertEqual(result['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())

    async def test_internal_task_root_cannot_bypass_worker_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / 'result.md'
            target.write_text('never read without auth')
            for authorization in (None, b'Bearer wrong-token'):
                with self.subTest(authorization=authorization):
                    request = self.read_request(target, root, authorization)
                    with mock.patch.object(direct, '_token', return_value='independent-test-token'), \
                         mock.patch.object(direct, 'read_file', side_effect=AssertionError('unauthenticated read')):
                        response = await direct.rpc(request)
                    self.assertEqual(response.status_code, 401)

    async def test_mcp_session_does_not_replace_bridge_auth(self):
        scope={'type':'http','method':'POST','path':'/bridge/rpc','headers':[(b'mcp-session-id',b'trusted')],'query_string':b''}
        with mock.patch.object(direct,'_token',return_value='independent-test-token'):
            response=await direct.rpc(Request(scope))
        self.assertEqual(response.status_code,401)
    async def test_repeated_request_id_runs_shell_once(self):
        def request(body):
            raw=json.dumps(body).encode()
            used=False
            async def receive():
                nonlocal used
                if not used:used=True;return {'type':'http.request','body':raw,'more_body':False}
                return {'type':'http.disconnect'}
            return Request({'type':'http','method':'POST','path':'/bridge/rpc','headers':[(b'authorization',b'Bearer independent-test-token')],'query_string':b''},receive)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);payload={'method':'exec','request_id':'same-id-test','params':{'cmd':'echo once','cwd':str(root)}}
            calls=0
            async def fake(*args,**kwargs):
                nonlocal calls;calls+=1
                return {'code':0,'output':'once\n','timed_out':False,'log_path':str(root/'log'),'total_chars':5,'duration_ms':1}
            with mock.patch.object(direct,'_token',return_value='independent-test-token'), mock.patch.object(direct,'run_command',fake):
                first=await direct.rpc(request(payload));second=await direct.rpc(request(payload))
            self.assertEqual(first.status_code,200);self.assertEqual(first.body,second.body);self.assertEqual(calls,1)
            direct._EXECUTIONS.pop('same-id-test',None)

if __name__=='__main__':unittest.main()
