"""Start the notion-bridge MCP server and (optionally) a cloudflare tunnel.

Usage:
  uv run python scripts/run.py            # bridge only, on 127.0.0.1:8760
  uv run python scripts/run.py --tunnel   # bridge + ephemeral cloudflare tunnel

--tunnel prints a https://<random>.trycloudflare.com URL. That URL + "/mcp"
is what you paste into Notion's custom MCP connection (with the bearer token
from .env). The URL changes every restart; for a stable one, use a named
tunnel on a domain you own.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import urllib.request


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=1):
                return True
        except Exception as e:
            if getattr(e, "code", None) in (400, 401, 405, 406):
                return True
            time.sleep(0.3)
    return False


def main() -> int:
    import uvicorn
    import notion_bridge.server as s

    port = s.PORT

    if "--tunnel" not in sys.argv:
        s.main()
        return 0

    app = s.build_app(s._TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", timeout_keep_alive=60))
    threading.Thread(target=server.run, daemon=True).start()
    if not wait_ready(port):
        print("[run] bridge did not become ready", file=sys.stderr)
        return 1
    print(f"[run] bridge ready on 127.0.0.1:{port}/mcp")

    cf_log = "/tmp/notion-bridge-cloudflared.log"
    open(cf_log, "w").close()
    cf = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=open(cf_log, "w"), stderr=subprocess.STDOUT,
    )
    url = None
    for _ in range(30):
        try:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(cf_log).read())
            if m:
                url = m.group(0)
                break
        except OSError:
            pass
        if cf.poll() is not None:
            break
        time.sleep(1)

    if not url:
        print("[run] could not get a tunnel URL; see", cf_log, file=sys.stderr)
        cf.terminate()
        return 1

    print("\n" + "=" * 64)
    print("  Notion custom MCP connection URL:")
    print(f"    {url}/mcp")
    print("  Auth: Header-based -> Authorization: Bearer <NOTION_BRIDGE_TOKEN>")
    print("  (token is in notion-bridge/.env)")
    print("=" * 64 + "\n")
    try:
        cf.wait()
    except KeyboardInterrupt:
        cf.terminate()
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
