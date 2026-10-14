"""Launch this tunnel with a physical-network source address on macOS.

The current TUN route fails TLS for Cloudflare's tunnel SNI. Source binding
changes only cloudflared's edge sockets, not system proxy configuration.
Resolve the DHCP address afresh on each launch rather than storing it in plist.
"""
import os
import shutil
import subprocess


def main():
    interface = os.environ.get('NOTION_TUNNEL_INTERFACE', 'en0')
    address = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface],
                             capture_output=True, text=True, check=False).stdout.strip()
    if not address:
        raise SystemExit(f'No IPv4 address on tunnel interface {interface}; launchd will retry')
    cloudflared = os.environ.get('CLOUDFLARED_BIN') or shutil.which('cloudflared')
    if not cloudflared:
        raise SystemExit('cloudflared not found on PATH (brew install cloudflared)')
    command = [cloudflared, 'tunnel', '--no-autoupdate',
               '--protocol', 'http2', '--edge', '198.41.192.227:7844',
               '--edge-bind-address', address, '--edge-ip-version', '4',
               '--metrics', '127.0.0.1:20241', '--url', 'http://127.0.0.1:8760']
    os.execv(command[0], command)


if __name__ == '__main__':
    main()
