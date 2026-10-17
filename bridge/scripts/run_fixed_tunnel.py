"""Supervise a named Cloudflare tunnel; launchd remains the outer supervisor.

Required environment: NOTION_FIXED_TUNNEL_CONFIG and NOTION_FIXED_TUNNEL_ID.
Optional: CLOUDFLARED_BIN (otherwise PATH), NOTION_TUNNEL_INTERFACE (en0),
NOTION_FIXED_TUNNEL_METRICS (127.0.0.1:20242), NOTION_FIXED_TUNNEL_EDGES
(comma-separated IPv4 addresses), NOTION_FIXED_TUNNEL_MAX_RETRIES (10).
Config paths expand ~ and resolve relative to the working directory. Credentials
and ingress belong in the caller's cloudflared config, never in this script.
"""
import concurrent.futures
import ipaddress
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


CANDIDATE_EDGES = (
    '198.41.192.4', '198.41.192.224', '198.41.192.225',
    '198.41.192.226', '198.41.192.227', '198.41.200.8',
    '198.41.200.9', '198.41.200.10', '198.41.200.11', '198.41.200.12',
)
STALE_SECONDS = 240
POLL_SECONDS = 15
PROBE_TIMEOUT = 6
STOP_TIMEOUT = 10


def log(message):
    print(f'[fixed-tunnel-wrapper] {time.strftime("%F %T")} {message}', flush=True)


def settings():
    config = os.environ.get('NOTION_FIXED_TUNNEL_CONFIG', '')
    tunnel = os.environ.get('NOTION_FIXED_TUNNEL_ID', '').strip()
    if not config or not tunnel or tunnel.startswith('-'):
        raise ValueError('set NOTION_FIXED_TUNNEL_CONFIG and NOTION_FIXED_TUNNEL_ID')
    config = Path(config).expanduser().resolve()
    if not config.is_file():
        raise ValueError('NOTION_FIXED_TUNNEL_CONFIG must name an existing file')
    binary = os.environ.get('CLOUDFLARED_BIN') or shutil.which('cloudflared')
    if not binary:
        raise ValueError('cloudflared not found on PATH (brew install cloudflared)')
    binary = os.path.expanduser(binary)
    edges = tuple(ip.strip() for ip in os.environ.get(
        'NOTION_FIXED_TUNNEL_EDGES', ','.join(CANDIDATE_EDGES)).split(','))
    for ip in edges:
        ipaddress.IPv4Address(ip)
    if len(edges) > 32:
        raise ValueError('NOTION_FIXED_TUNNEL_EDGES supports at most 32 addresses')
    metrics = os.environ.get('NOTION_FIXED_TUNNEL_METRICS', '127.0.0.1:20242')
    host, port = metrics.rsplit(':', 1)
    if not ipaddress.IPv4Address(host).is_loopback or not 1 <= int(port) <= 65535:
        raise ValueError('NOTION_FIXED_TUNNEL_METRICS must be a loopback IPv4:port')
    retries = int(os.environ.get('NOTION_FIXED_TUNNEL_MAX_RETRIES', '10'))
    if not 1 <= retries <= 100:
        raise ValueError('NOTION_FIXED_TUNNEL_MAX_RETRIES must be between 1 and 100')
    return dict(binary=binary, config=str(config), tunnel=tunnel, edges=edges,
                metrics=metrics, retries=retries,
                interface=os.environ.get('NOTION_TUNNEL_INTERFACE', 'en0'))


def current_address(interface):
    try:
        return subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface],
                              capture_output=True, text=True, check=False,
                              timeout=PROBE_TIMEOUT).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def edge_reachable(ip, source):
    # Match the private runner's numeric-SNI TLS reachability probe. No tunnel
    # credentials are sent; cloudflared performs its own authenticated TLS.
    try:
        with socket.create_connection((ip, 7844), timeout=PROBE_TIMEOUT,
                                      source_address=(source, 0) if source else None) as raw:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with context.wrap_socket(raw, server_hostname=ip):
                return True
    except OSError:
        return False


def pick_edges(source, candidates):
    with concurrent.futures.ThreadPoolExecutor(len(candidates)) as pool:
        reachable = list(pool.map(lambda ip: edge_reachable(ip, source), candidates))
    return [ip for ip, ok in zip(candidates, reachable) if ok][:2]


def ready_connections(metrics):
    # Ignore proxy environment variables for the local readiness endpoint.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://{metrics}/ready', timeout=5) as response:
            count = json.load(response).get('readyConnections', 0)
        return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0
    except (OSError, ValueError, AttributeError):
        return 0


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT)
    else:
        process.wait(timeout=STOP_TIMEOUT)


def run_once(options, edges, address, stop):
    command = [options['binary'], 'tunnel', '--config', options['config'],
               '--no-autoupdate', '--protocol', 'http2']
    for edge in edges:
        command += ['--edge', f'{edge}:7844']
    command += ['--edge-ip-version', '4']
    if address:
        command += ['--edge-bind-address', address]
    command += ['--metrics', options['metrics'], 'run', options['tunnel']]
    log(f'launching edges {edges} bind {address or "default route"}')
    process = subprocess.Popen(command)
    healthy = False
    deadline = time.monotonic() + STALE_SECONDS
    try:
        while process.poll() is None and not stop.wait(POLL_SECONDS):
            if ready_connections(options['metrics']) > 0:
                healthy = True
                deadline = time.monotonic() + STALE_SECONDS
            elif time.monotonic() >= deadline:
                log('no registered edge within grace window; reprobe network')
                break
        return healthy
    finally:
        stop_child(process)


def supervise(options, stop):
    failures = 0
    while not stop.is_set() and failures < options['retries']:
        address = current_address(options['interface'])
        healthy = False
        sources = (address, None) if address else (None,)
        for source in sources:
            edges = pick_edges(source, options['edges'])
            if stop.is_set():
                break
            if not edges:
                continue
            try:
                healthy = run_once(options, edges, source, stop)
            except OSError as exc:
                log(f'could not launch cloudflared: {exc}')
            # A successful probe can still fail registration. Try the default
            # route if the bound child never became ready, before reprobing.
            if healthy or stop.is_set():
                break
        if not healthy and not stop.is_set():
            log('no registered Cloudflare edge on current network')
        failures = 0 if healthy else failures + 1
        if failures < options['retries']:
            stop.wait(POLL_SECONDS)
    if stop.is_set():
        return 0
    log('consecutive retry limit reached; outer supervisor may retry')
    return 1


def main():
    try:
        options = settings()
    except (ValueError, OSError) as exc:
        log(str(exc))
        return 2
    stop = threading.Event()
    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        return supervise(options, stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
