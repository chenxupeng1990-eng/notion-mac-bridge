"""Cache immutable tool definitions; retry discovery only, never execution."""
from __future__ import annotations

import asyncio
import copy
from mcp.server.mcpserver import MCPServer


class CachedMCPServer(MCPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._discovery_cache = None
        self._discovery_lock = asyncio.Lock()

    async def list_tools(self):
        if self._discovery_cache is not None:
            return copy.deepcopy(self._discovery_cache)
        async with self._discovery_lock:
            if self._discovery_cache is None:
                for attempt in range(3):
                    try:
                        tools = await asyncio.wait_for(super().list_tools(), timeout=60)
                        self._discovery_cache = copy.deepcopy(tools)
                        break
                    except (TimeoutError, OSError):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(0.2 * (attempt + 1))
        return copy.deepcopy(self._discovery_cache)
