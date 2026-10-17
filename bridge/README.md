# Local Notion MCP bridge

Python package for the local channel of [notion-mac-bridge](../README.md).
Deployment, authentication, dependencies and tool usage are documented in the
[repository tutorial](../docs/TUTORIAL.md). Copy `config.json.example` to
`config.json`, configure your workspace, and keep `NOTION_BRIDGE_TOKEN` in a
private `.env` file before starting the server.

```bash
uv sync
```

```bash
uv run python scripts/run.py
```

The server binds to loopback. To connect a cloud client, use an authenticated
Cloudflare tunnel as described in the tutorial. Shell commands run with the
local user's permissions; the bridge is not a sandbox.
