"""Read-only discovery retry plus explicit one-shot tool calls for acceptance."""
import argparse
import asyncio
import hashlib
import json
import os
import random
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.environ.get('NOTION_WORKSPACE_DIR', str(Path.home() / 'notion-workspace'))) / 'artifacts' / 'mcp-acceptance'


class MCPClient:
    def __init__(self, url, token):
        self.url = url
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=30), limits=httpx.Limits(keepalive_expiry=60))
        self.serial = 0
        self.cache = None
        self.discovery_retries = 0

    async def rpc(self, method, params=None, notification=False):
        self.serial += 1
        body = {'jsonrpc': '2.0', 'method': method}
        if not notification: body['id'] = self.serial
        if params is not None: body['params'] = params
        response = await self.client.post(self.url, headers=self.headers, json=body)
        response.raise_for_status()
        if notification or not response.content: return {}
        if response.headers.get('mcp-session-id'):
            self.headers['mcp-session-id'] = response.headers['mcp-session-id']
        text = response.text
        result = json.loads(text) if response.headers.get('content-type','').startswith('application/json') else json.loads(next(line[5:].strip() for line in text.splitlines() if line.startswith('data:')))
        if 'error' in result: raise RuntimeError(str(result['error']))
        return result['result']

    async def initialize(self):
        await self.rpc('initialize', {'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'mcp-acceptance','version':'1'}})
        await self.rpc('notifications/initialized', notification=True)

    async def tools(self, refresh=False):
        if self.cache and not refresh and time.monotonic()-self.cache[0] < 60: return self.cache[1]
        for attempt in range(3):
            try:
                tools = await self.rpc('tools/list',{})
                self.cache = (time.monotonic(),tools)
                return tools
            except (httpx.TransportError,httpx.HTTPStatusError,RuntimeError):
                if attempt == 2: raise
                self.discovery_retries += 1
                await asyncio.sleep(0.3 * 2**attempt)
        raise AssertionError('unreachable')

    async def call(self, name, args):
        # Never automatically retry an execution call.
        result=await self.rpc('tools/call',{'name':name,'arguments':args})
        if result.get('isError'): raise RuntimeError(str(result.get('content')))
        return result

    async def close(self): await self.client.aclose()


def env_token():
    data=dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    return data['NOTION_BRIDGE_TOKEN']


async def continuity(url):
    client=MCPClient(url,env_token());report={'url':url,'calls':[],'started_at':time.strftime('%Y-%m-%d %H:%M:%S'),'discovery_timeout_seconds':180}
    rng=random.Random(20261014)
    try:
        await client.initialize()
        for index in range(50):
            gap=rng.randint(0,30) if index else 0
            if gap: await asyncio.sleep(gap)
            started=time.monotonic()
            tools=await client.tools(refresh=True)
            assert any(t['name']=='exec' for t in tools['tools'])
            marker='ok'
            result=await client.call('exec',{'cmd':'echo ok'})
            text='\n'.join(c.get('text','') for c in result['content'])
            assert marker in text and 'timed_out: false' in text
            row={'index':index+1,'gap_seconds':gap,'response_seconds':round(time.monotonic()-started,3),'ok':True}
            report['calls'].append(row)
            OUTPUT.mkdir(parents=True,exist_ok=True)
            (OUTPUT/'continuity.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(f"call {index+1}/50 gap={gap}s OK",flush=True)
        report['discovery_retries']=client.discovery_retries;report['passed']=True
    except Exception as exc:
        report['passed']=False;report['error']=str(exc);raise
    finally:
        await client.close();OUTPUT.mkdir(parents=True,exist_ok=True)
        (OUTPUT/'continuity.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);args=p.parse_args()
    asyncio.run(continuity(args.url))
