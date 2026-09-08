"""A real Codex executes a file write once, then resumes across a mid-stream 429."""
import asyncio
import json
import os
import shutil
from dataclasses import replace

import httpx
import pytest
import tomlkit

from tests.test_proxy import live_server, response, settings, TOKEN
from tests.test_reconnect import event
from tests.test_codex_reconnect import generation


@pytest.mark.skipif(shutil.which('codex') is None, reason='Requires the installed Codex CLI')
async def test_real_codex_route_preserves_completed_tool_after_rate_limit(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    codex_home=tmp_path/'codex';codex_home.mkdir()
    calls=[];bodies=[]
    async def handler(request):
        calls.append(request.url.host)
        body=json.loads(request.content);bodies.append(body)
        if len(calls)==1:
            initial={'id':'resp_tool','object':'response','status':'in_progress','output':[]}
            arguments=json.dumps({'cmd':"Add-Content -LiteralPath counter.txt -Value ONCE",'workdir':str(workspace),'max_output_tokens':1000})
            item={'id':'fc_once','type':'function_call','name':'exec_command','call_id':'call_once','arguments':arguments,'status':'completed'}
            return response(chunks=[event('response.created',sequence_number=0,response=initial),
                event('response.output_item.added',sequence_number=1,output_index=0,item={**item,'arguments':'','status':'in_progress'}),
                event('response.function_call_arguments.delta',sequence_number=2,output_index=0,item_id='fc_once',delta=arguments),
                event('response.function_call_arguments.done',sequence_number=3,output_index=0,item_id='fc_once',arguments=arguments),
                event('response.output_item.done',sequence_number=4,output_index=0,item=item),
                event('response.completed',sequence_number=5,response={**initial,'status':'completed','output':[item],
                      'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}})],headers={'content-type':'text/event-stream'})
        return response(chunks=generation(len(calls),'rate_limit' if len(calls)==2 else None),headers={'content-type':'text/event-stream'})
    config=settings(reconnect_failover=True,rotate_on_failure=True,reconnect_cooldown_seconds=60)
    config=replace(config,providers=tuple(replace(p,deployment='gpt-6-astra') for p in config.providers))
    async with live_server(handler,configuration=config) as (url,runtime):
        runtime.set_route({'id':'tool-test','providers':['provider2','provider1','provider3']})
        setup={'model':'gpt-6-astra','model_provider':'test_proxy','model_reasoning_effort':'low','approval_policy':'never','sandbox_mode':'danger-full-access',
               'model_providers':{'test_proxy':{'name':'Local test','base_url':url+'/r/tool-test/v1','env_key':'TEST_PROXY_TOKEN','wire_api':'responses',
                 'request_max_retries':0,'stream_max_retries':3,'stream_idle_timeout_ms':10000,'supports_websockets':False}},
               'features':{'enable_request_compression':False,'responses_websockets':False,'responses_websockets_v2':False}}
        (codex_home/'config.toml').write_text(tomlkit.dumps(setup),encoding='utf-8')
        process=await asyncio.create_subprocess_exec(shutil.which('codex'),'exec','--ephemeral','--skip-git-repo-check','--sandbox','danger-full-access','--json',
            'Write ONCE to counter.txt using exec_command exactly once; then report completion.',cwd=workspace,
            env={**os.environ,'CODEX_HOME':str(codex_home),'TEST_PROXY_TOKEN':TOKEN},stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            out,err=await asyncio.wait_for(process.communicate(),60)
        finally:
            if process.returncode is None:
                if os.name=='nt':
                    killer=await asyncio.create_subprocess_exec('taskkill','/PID',str(process.pid),'/T','/F',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                else:process.kill()
                await process.wait()
        diagnostic=(out+err).decode(errors='replace')
        assert process.returncode==0,diagnostic
        assert (workspace/'counter.txt').read_text().splitlines()==['ONCE'],diagnostic
        assert calls==['p2.example','p2.example','p1.example'],diagnostic
        for body in bodies[1:]:
            assert any(i.get('type')=='function_call_output' and i.get('call_id')=='call_once' for i in body.get('input',[])),body.get('input')
        assert runtime.stats['reconnect_failovers']==1
        assert 'RECOVERED' in diagnostic
