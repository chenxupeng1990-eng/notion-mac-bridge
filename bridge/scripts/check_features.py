"""Exercise upgraded local tools through the public MCP transport."""
import asyncio
import base64
import os
import sys
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

from PIL import Image
import numpy as np
from check_mcp_acceptance import MCPClient, env_token, OUTPUT

WORKSPACE=Path(os.environ.get('NOTION_WORKSPACE_DIR', str(Path.home() / 'notion-workspace')))
URL=os.environ.get('NOTION_BRIDGE_URL') or sys.exit('set NOTION_BRIDGE_URL=https://<your-tunnel>.trycloudflare.com/mcp')


def text(reply):
    return '\n'.join(part.get('text','') for part in reply['content'])


async def main():
    client=MCPClient(URL,env_token());report={}
    OUTPUT.mkdir(parents=True,exist_ok=True)
    try:
        await client.initialize();tools=await client.tools()
        report['tools_count']=len(tools['tools'])
        answer=text(await client.call('exec',{'cmd':'seq 1 200000'}))
        logfile=Path(re.search(r'^log_path: (.+)$',answer,re.M)[1])
        expected=''.join(f'{i}\n' for i in range(1,200001)).encode()
        assert logfile.read_bytes()==expected and '\n1\n2\n' in answer and answer.endswith('200000\n') and '[... 截断 ' in answer
        report['exec']={'passed':True,'log_path':str(logfile),'lines':200000,'bytes':len(expected),'sha256':hashlib.sha256(expected).hexdigest(),'preview':answer}
        for name,content in [('a.py','print("你好")\n'),('settings.yaml','name: notion\n')]:
            p=WORKSPACE/'projects/test'/name
            answer=text(await client.call('fs_write',{'path':str(p),'content':content}))
            sha=subprocess.check_output(['shasum','-a','256',str(p)],text=True).split()[0]
            assert sha in answer and f'({len(content.encode())} bytes)' in answer
            report[name]={'passed':True,'sha256':sha,'response':answer}
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w:gz') as archive:
            data=b'notion archive acceptance\n';info=tarfile.TarInfo('test.txt');info.size=len(data);archive.addfile(info,io.BytesIO(data))
        raw=buf.getvalue();p=WORKSPACE/'projects/test/test.tgz'
        answer=text(await client.call('write_binary',{'path':str(p),'base64_data':base64.b64encode(raw).decode()}))
        sha=subprocess.check_output(['shasum','-a','256',str(p)],text=True).split()[0]
        assert p.read_bytes()==raw and sha in answer
        report['tgz']={'passed':True,'sha256':sha,'response':answer}
        rejected=text(await client.call('fs_write',{'path':'~/../etc/x.py','content':'test'}));assert '[rejected]' in rejected
        target=OUTPUT/'outside';target.mkdir(exist_ok=True);link=WORKSPACE/'projects/test/escape'
        if not link.exists() and not link.is_symlink():link.symlink_to(target,target_is_directory=True)
        rejected2=text(await client.call('fs_write',{'path':str(link/'bad.py'),'content':'test'}));assert '[rejected]' in rejected2 and not (target/'bad.py').exists()
        report['path_rejection']={'passed':True,'traversal':rejected,'symlink':rejected2}
        source=OUTPUT/'noise-2600x2000.png'
        Image.fromarray(np.random.default_rng(7).integers(0,256,(2000,2600,3),dtype=np.uint8)).save(source)
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        answer=await client.call('view_image',{'path':str(source)})
        image=next(c for c in answer['content'] if c['type']=='image');raw=base64.b64decode(image['data']);preview=Image.open(io.BytesIO(raw))
        assert preview.width<=1024 and len(raw)<=300000 and before==hashlib.sha256(source.read_bytes()).hexdigest()
        report['image']={'passed':True,'original_dimensions':[2600,2000],'preview_dimensions':list(preview.size),'bytes':len(raw),'mime':image['mimeType'],'source_sha256':before}
        (OUTPUT/'preview.jpg').write_bytes(raw)
        answer=text(await client.call('exec',{'cmd':'echo partial-output; sleep 60','timeout_seconds':1}))
        assert 'timed_out: true' in answer and 'partial-output' in answer
        report['timeout']={'passed':True,'response':answer}
        answer=text(await client.call('exec',{'cmd':'seq 1 100','max_output_chars':20,'background':True}))
        jid=re.search(r'started (job-[a-f0-9]+)',answer)[1]
        for _ in range(20):
            reply=text(await client.call('job_status',{'job_id':jid}))
            if reply.startswith('done '):break
            await asyncio.sleep(.1)
        assert '截断' in reply and reply.endswith('100\n')
        report['background']={'passed':True,'response':reply}
        answer=text(await client.call('exec',{'cmd':f"{WORKSPACE}/.venv/bin/python -c 'import numpy,PIL; print(numpy.__version__,PIL.__version__)'; ffmpeg -hide_banner -filters 2>/dev/null | grep drawtext"}))
        assert 'drawtext' in answer and '2.5.4' in answer
        report['environment']={'passed':True,'response':answer}
        report['passed']=True
        print(json.dumps({key:value for key,value in report.items() if key not in ('exec','background')},ensure_ascii=False,indent=2))
    finally:
        (OUTPUT/'local-features.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        await client.close()

if __name__=='__main__':asyncio.run(main())
