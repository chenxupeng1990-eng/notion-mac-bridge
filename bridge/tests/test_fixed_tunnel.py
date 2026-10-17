"""Offline tests: never start cloudflared or probe a real network."""
import importlib.util
import io
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


spec = importlib.util.spec_from_file_location(
    'run_fixed_tunnel', Path(__file__).resolve().parents[1] / 'scripts' / 'run_fixed_tunnel.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FixedTunnelTests(unittest.TestCase):
    def setUp(self):
        self.options = dict(binary='/tools/cloudflared', config='/config/tunnel.yml',
                            tunnel='test-tunnel', metrics='127.0.0.1:20242',
                            edges=('192.0.2.1', '192.0.2.2'), interface='en0', retries=2)
        patcher = mock.patch.object(runner, 'log')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_environment_contract_and_path_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'tunnel.yml'
            config.touch()
            env = {'HOME': temp, 'NOTION_FIXED_TUNNEL_CONFIG': '~/tunnel.yml',
                   'NOTION_FIXED_TUNNEL_ID': 'test-tunnel'}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(runner.shutil, 'which', return_value='/tools/cloudflared'):
                options = runner.settings()
                self.assertEqual(options['config'], str(config.resolve()))
                self.assertEqual(options['binary'], '/tools/cloudflared')
                self.assertEqual(options['retries'], 10)
                self.assertEqual(options['metrics'], '127.0.0.1:20242')
                with mock.patch.dict(os.environ, {'CLOUDFLARED_BIN': '~/bin/cloudflared',
                                                  'NOTION_FIXED_TUNNEL_EDGES': '192.0.2.1, 192.0.2.2'}):
                    options = runner.settings()
                    self.assertEqual(options['binary'], str(Path(temp) / 'bin/cloudflared'))
                    self.assertEqual(options['edges'], ('192.0.2.1', '192.0.2.2'))
                for overrides in ({'NOTION_FIXED_TUNNEL_ID': ''},
                                  {'NOTION_FIXED_TUNNEL_ID': '--bad'},
                                  {'NOTION_FIXED_TUNNEL_CONFIG': '/absent/config.yml'},
                                  {'NOTION_FIXED_TUNNEL_EDGES': ''},
                                  {'NOTION_FIXED_TUNNEL_EDGES': 'edge.example'},
                                  {'NOTION_FIXED_TUNNEL_METRICS': '0.0.0.0:20242'},
                                  {'NOTION_FIXED_TUNNEL_METRICS': '127.0.0.1:0'},
                                  {'NOTION_FIXED_TUNNEL_MAX_RETRIES': '0'},
                                  {'NOTION_FIXED_TUNNEL_MAX_RETRIES': '101'}):
                    with self.subTest(overrides=overrides), mock.patch.dict(os.environ, overrides):
                        with self.assertRaises(ValueError):
                            runner.settings()
                with mock.patch.object(runner.shutil, 'which', return_value=None):
                    with self.assertRaisesRegex(ValueError, 'PATH'):
                        runner.settings()

    def test_address_lookup_is_bounded(self):
        with mock.patch.object(runner.subprocess, 'run') as run:
            run.return_value.stdout = '192.0.2.10\n'
            self.assertEqual(runner.current_address('en9'), '192.0.2.10')
            run.assert_called_once_with(['/usr/sbin/ipconfig', 'getifaddr', 'en9'],
                                        capture_output=True, text=True, check=False,
                                        timeout=runner.PROBE_TIMEOUT)
            run.side_effect = subprocess.TimeoutExpired('ipconfig', 6)
            self.assertEqual(runner.current_address('en9'), '')

    def test_probe_checks_tls_with_bound_and_default_source(self):
        with mock.patch.object(runner.socket, 'create_connection') as connect, \
             mock.patch.object(runner.ssl, 'SSLContext') as context:
            self.assertTrue(runner.edge_reachable('192.0.2.1', '192.0.2.10'))
            connect.assert_called_with(('192.0.2.1', 7844), timeout=6,
                                       source_address=('192.0.2.10', 0))
            context.return_value.wrap_socket.assert_called_with(
                connect.return_value.__enter__.return_value, server_hostname='192.0.2.1')
            self.assertTrue(runner.edge_reachable('192.0.2.1', None))
            self.assertIsNone(connect.call_args.kwargs['source_address'])
            context.return_value.wrap_socket.side_effect = OSError('TLS stalled')
            self.assertFalse(runner.edge_reachable('192.0.2.1', None))

    def test_selects_first_two_reachable_edges(self):
        candidates = ('192.0.2.1', '192.0.2.2', '192.0.2.3', '192.0.2.4')
        with mock.patch.object(runner, 'edge_reachable', side_effect=lambda ip, source: ip != candidates[0]) as probe:
            self.assertEqual(runner.pick_edges('192.0.2.10', candidates), list(candidates[1:3]))
            self.assertEqual(probe.call_count, 4)

    def test_readiness_failure_and_malformed_values_are_unready(self):
        with mock.patch.object(runner.urllib.request, 'build_opener') as build:
            response = build.return_value.open.return_value.__enter__
            for payload, expected in ((b'{"readyConnections":2}', 2), (b'{}', 0),
                                      (b'{"readyConnections":"2"}', 0),
                                      (b'{"readyConnections":true}', 0),
                                      (b'{"readyConnections":-1}', 0), (b'[]', 0), (b'bad', 0)):
                response.return_value = io.BytesIO(payload)
                self.assertEqual(runner.ready_connections('127.0.0.1:20242'), expected)
            build.return_value.open.assert_called_with('http://127.0.0.1:20242/ready', timeout=5)
            build.return_value.open.side_effect = OSError('unavailable')
            self.assertEqual(runner.ready_connections('127.0.0.1:20242'), 0)

    def test_child_grace_window_resets_and_stale_child_is_reaped(self):
        process = mock.Mock()
        process.poll.return_value = None
        stop = mock.Mock()
        stop.wait.return_value = False
        with mock.patch.object(runner.subprocess, 'Popen', return_value=process) as launch, \
             mock.patch.object(runner, 'ready_connections', side_effect=[1, 0, 0]), \
             mock.patch.object(runner.time, 'monotonic', side_effect=[0, 100, 250, 341]):
            self.assertTrue(runner.run_once(self.options, ['192.0.2.1'], '192.0.2.10', stop))
        command = launch.call_args.args[0]
        self.assertEqual(command[-4:], ['--metrics', '127.0.0.1:20242', 'run', 'test-tunnel'])
        self.assertIn('--edge-bind-address', command)
        self.assertIn('/config/tunnel.yml', command)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)

    def test_stop_and_exceptions_cleanup_child_with_kill_fallback(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                process = mock.Mock()
                process.poll.return_value = None
                process.wait.side_effect = [subprocess.TimeoutExpired('cloudflared', 10), 0]
                stop = mock.Mock()
                stop.wait.return_value = not raises
                with mock.patch.object(runner.subprocess, 'Popen', return_value=process) as launch, \
                     mock.patch.object(runner, 'ready_connections', side_effect=RuntimeError('unexpected')):
                    if raises:
                        with self.assertRaises(RuntimeError):
                            runner.run_once(self.options, ['192.0.2.1'], None, stop)
                    else:
                        self.assertFalse(runner.run_once(self.options, ['192.0.2.1'], None, stop))
                    self.assertNotIn('--edge-bind-address', launch.call_args.args[0])
                process.terminate.assert_called_once()
                process.kill.assert_called_once()
                self.assertEqual(process.wait.call_count, 2)

    def test_exited_child_is_reaped(self):
        process = mock.Mock()
        process.poll.return_value = 1
        with mock.patch.object(runner.subprocess, 'Popen', return_value=process):
            self.assertFalse(runner.run_once(self.options, ['192.0.2.1'], None, threading.Event()))
        process.terminate.assert_not_called()
        process.wait.assert_called_once_with(timeout=10)

    def test_bound_edges_preferred_and_address_refreshed_on_recovery(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        with mock.patch.object(runner, 'current_address', side_effect=['192.0.2.10', '192.0.2.11']), \
             mock.patch.object(runner, 'pick_edges', return_value=['192.0.2.1']) as pick, \
             mock.patch.object(runner, 'run_once', return_value=False) as run:
            self.assertEqual(runner.supervise(self.options, stop), 1)
        self.assertEqual(pick.call_args_list, [mock.call('192.0.2.10', self.options['edges']),
                                              mock.call(None, self.options['edges']),
                                              mock.call('192.0.2.11', self.options['edges']),
                                              mock.call(None, self.options['edges'])])
        self.assertEqual([call.args[2] for call in run.call_args_list],
                         ['192.0.2.10', None, '192.0.2.11', None])
        stop.wait.assert_called_once_with(15)

    def test_successful_bound_child_does_not_probe_default_route(self):
        stop = threading.Event()
        def launched(*args):
            stop.set()
            return True
        with mock.patch.object(runner, 'current_address', return_value='192.0.2.10'), \
             mock.patch.object(runner, 'pick_edges', return_value=['192.0.2.1']) as pick, \
             mock.patch.object(runner, 'run_once', side_effect=launched):
            self.assertEqual(runner.supervise(self.options, stop), 0)
            pick.assert_called_once_with('192.0.2.10', self.options['edges'])

    def test_default_route_fallback_and_missing_address(self):
        for address, probes in (('192.0.2.10', [[], ['192.0.2.2']]), ('', [['192.0.2.2']])):
            stop = threading.Event()
            def launched(options, edges, source, stop):
                self.assertIsNone(source)
                self.assertEqual(edges, ['192.0.2.2'])
                stop.set()
                return False
            with self.subTest(address=address), \
                 mock.patch.object(runner, 'current_address', return_value=address), \
                 mock.patch.object(runner, 'pick_edges', side_effect=probes), \
                 mock.patch.object(runner, 'run_once', side_effect=launched):
                self.assertEqual(runner.supervise(self.options, stop), 0)

    def test_no_network_and_failed_launch_have_bounded_retries(self):
        for reachable in (False, True):
            stop = mock.Mock()
            stop.is_set.return_value = False
            with self.subTest(reachable=reachable), \
                 mock.patch.object(runner, 'current_address', return_value=''), \
                 mock.patch.object(runner, 'pick_edges', return_value=['192.0.2.1'] if reachable else []) as pick, \
                 mock.patch.object(runner, 'run_once', side_effect=OSError('cannot execute')):
                self.assertEqual(runner.supervise(self.options, stop), 1)
                self.assertEqual(pick.call_count, 2)
                stop.wait.assert_called_once_with(15)

    def test_healthy_child_resets_failure_budget(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        with mock.patch.object(runner, 'current_address', return_value=''), \
             mock.patch.object(runner, 'pick_edges', return_value=['192.0.2.1']), \
             mock.patch.object(runner, 'run_once', side_effect=[False, True, False, False]) as run:
            self.assertEqual(runner.supervise(self.options, stop), 1)
            self.assertEqual(run.call_count, 4)

    def test_stop_during_probe_prevents_child_launch(self):
        stop = threading.Event()
        def probe(*args):
            stop.set()
            return ['192.0.2.1']
        with mock.patch.object(runner, 'current_address', return_value='192.0.2.10'), \
             mock.patch.object(runner, 'pick_edges', side_effect=probe), \
             mock.patch.object(runner, 'run_once') as run:
            self.assertEqual(runner.supervise(self.options, stop), 0)
            run.assert_not_called()

    def test_configuration_failure_exits_before_supervision(self):
        with mock.patch.object(runner, 'settings', side_effect=ValueError('missing config')), \
             mock.patch.object(runner, 'supervise') as supervise:
            self.assertEqual(runner.main(), 2)
            supervise.assert_not_called()

    def test_signals_request_stop_and_restore_handlers(self):
        handlers = {}
        def install(signum, handler):
            handlers[signum] = handler
            return signal.SIG_DFL
        def supervise(options, stop):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
            return 0
        with mock.patch.object(runner, 'settings', return_value=self.options), \
             mock.patch.object(runner.signal, 'signal', side_effect=install) as install_mock, \
             mock.patch.object(runner, 'supervise', side_effect=supervise):
            self.assertEqual(runner.main(), 0)
            self.assertEqual(install_mock.call_count, 4)
            self.assertEqual(handlers, {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL})


if __name__ == '__main__':
    unittest.main()
