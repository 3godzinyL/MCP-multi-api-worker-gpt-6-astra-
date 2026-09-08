import asyncio
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.experiment import prepare_copies, merge_copies, validate_plan
from dashboard.preferences import preferences, validate_preferences, read_agents, write_agents
from dashboard.runner import CodexRunner
from dashboard.server import create_app
from dashboard.store import DashboardStore
from proxy.catalog import ProviderCatalog
from proxy.config import load_config
from tests.test_dashboard import make_runner, wait_for
from tests.test_proxy import client_for, live_server, response, settings, HEADERS
from tests.test_reconnect import CREATED, DELTA, COMPLETE, RATE_LIMIT


@pytest.fixture
def vault(monkeypatch):
    values = {f"provider{i}": f"fixture-key-{i}" for i in range(1,4)}
    values["local-proxy-token"] = "fixture-token"
    for module in ("proxy.catalog", "proxy.server", "dashboard.runner", "dashboard.server"):
        monkeypatch.setattr(module + ".get_secret", lambda name, *args: values.get(name))
    monkeypatch.setattr("proxy.catalog.set_secret", lambda name, value: values.__setitem__(name, value))
    monkeypatch.setattr("proxy.catalog.delete_secret", lambda name: values.pop(name, None))
    return values


def configuration(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text('[proxy]\npublic_model="fixture-model"\n' + "\n".join(
        f'[[providers]]\nid="provider{i}"\nbase_url="https://p{i}.example/v1"\ndeployment="fixture-model"\n' for i in range(1,4)), encoding="utf-8")
    return path


def test_tabs_share_valid_session_and_search_filters_before_limit(tmp_path, vault):
    path=configuration(tmp_path)
    for i in range(165):
        (tmp_path / f"Folder-{i:03}").mkdir()
    (tmp_path / "Mega Informatyk").mkdir()
    app=create_app(data_dir=tmp_path/"data",config_path=path,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))
    with TestClient(app,base_url="http://127.0.0.1:4001") as client:
        first=client.get('/ui/').text
        csrf=re.search('name="csrf-token" content="([^"]+)"',first)[1]
        assert csrf in client.get('/ui/').text
        headers={"origin":"http://127.0.0.1:4001","x-panel-csrf":csrf}
        # A malformed body must reach application validation, not fail CSRF.
        assert client.post('/ui/api/projects',json={},headers=headers).status_code==400
        assert client.post('/ui/api/projects',json={},headers={**headers,"x-panel-csrf":"old"}).json()['code']=='csrf_expired'
        result=client.get('/ui/api/folders',params={"path":str(tmp_path),"q":"mEgA informatyk"}).json()
        assert result['total']==1 and result['folders'][0]['name']=='Mega Informatyk'
        assert '__COMPOSER__' not in first and 'value="max" selected' in first


def test_provider_add_edit_remove_vault_and_azure_url(tmp_path, vault):
    path=configuration(tmp_path);catalog=ProviderCatalog(path)
    updated,pid=catalog.change({"label":"Fourth API","base_url":"https://example.services.ai.azure.com/api/projects/demo","deployment":"fixture-model","key":"new-fixture-secret"})
    assert len(updated.providers)==4 and updated.providers[-1].base_url.endswith('/openai/v1')
    assert vault[pid]=='new-fixture-secret' and 'new-fixture-secret' not in path.read_text()
    catalog.change({"id":pid,"label":"Edited","key":""})
    assert vault[pid]=='new-fixture-secret'
    catalog.change({"id":pid,"key":"replaced-secret"})
    assert vault[pid]=='replaced-secret'
    catalog.change({"id":pid,"delete":True})
    assert pid not in vault and len(load_config(path).providers)==3
    assert all('secret' not in p.read_text() for p in (tmp_path/'backups/provider-edits').glob('*.toml'))


def test_invalid_provider_does_not_touch_keys_or_config(tmp_path, vault):
    path=configuration(tmp_path);before=path.read_bytes();keys=dict(vault)
    with pytest.raises(ValueError):
        ProviderCatalog(path).change({"base_url":"http://unsafe.example/v1","deployment":"model","key":"new-secret"})
    assert path.read_bytes()==before and vault==keys


def test_preferences_and_agents_version_conflict(tmp_path):
    store=DashboardStore(tmp_path/'data/panel.sqlite3')
    root=tmp_path/'project';root.mkdir();project=store.add_project(str(root))
    try:
        prefs=preferences(store)
        assert prefs['permission_mode']=='approval' and prefs['effort']=='max'
        with pytest.raises(ValueError):
            validate_preferences({'context_window':10000,'compact_at':10000},prefs)
        old=read_agents(project)
        first=write_agents(project,'Project rules\n',old['version'],tmp_path/'data')
        (root/'AGENTS.md').write_text('User edited this\n')
        with pytest.raises(ValueError,match='zmienił'):
            write_agents(project,'would overwrite',first['version'],tmp_path/'data')
        assert (root/'AGENTS.md').read_text()=='User edited this\n'
    finally:store.close()


async def test_explicit_yolo_is_a_real_protocol_choice(tmp_path, vault):
    store,runner,project=make_runner(tmp_path)
    seen=[];request=runner.request
    async def capture(method,params,**kwargs):
        seen.append((method,params));return await request(method,params,**kwargs)
    runner.request=capture
    try:
        result=await runner.start_task(project['id'],'approve','max',options={'permission_mode':'yolo'})
        task=runner.tasks[result['id']]
        await wait_for(lambda:task['state']=='completed')
        start=next(p for m,p in seen if m=='thread/start');turn=next(p for m,p in seen if m=='turn/start')
        assert start['sandbox']=='danger-full-access' and start['approvalPolicy']=='never'
        assert turn['sandboxPolicy']=={'type':'dangerFullAccess'} and turn['effort']=='max'
        assert not task['approvals']
    finally:await runner.close();store.close()


async def test_route_selects_primary_keeps_history_and_fails_over_to_reserve():
    calls=[];bodies=[]
    async def handler(request):
        calls.append(request.url.host);bodies.append(json.loads(request.content))
        return response(429,headers={'retry-after':'60'}) if request.url.host=='p2.example' else response(chunks=[b'{}'])
    config=settings(reconnect_failover=True,rotate_on_failure=True)
    config=replace(config,providers=tuple(replace(p,deployment='same-model') for p in config.providers))
    async with client_for(handler,configuration=config) as (client,runtime):
        runtime.set_route({'id':'task','providers':['provider2','provider1']})
        payload={'model':'same-model','input':[{'role':'user','content':'keep this prompt'},{'type':'function_call_output','call_id':'old-call','output':'already executed'}]}
        result=await client.post('/r/task/v1/responses',json=payload)
        assert result.status_code==200 and calls==['p2.example','p1.example']
        assert bodies[0]==bodies[1]==payload
        assert runtime.routes['task']['last_provider']=='provider1'
        assert runtime.preferred_index==0


async def test_route_stream_failure_reconnects_without_splicing():
    calls=[]
    async def handler(request):
        calls.append(request.url.host)
        return response(chunks=[CREATED,DELTA,RATE_LIMIT] if len(calls)==1 else [CREATED,COMPLETE],headers={'content-type':'text/event-stream'})
    config=settings(reconnect_failover=True,rotate_on_failure=True)
    config=replace(config,providers=tuple(replace(p,deployment='same-model') for p in config.providers))
    async with client_for(handler,configuration=config) as (client,runtime):
        runtime.set_route({'id':'worker','providers':['provider2','provider1']})
        first=await client.post('/r/worker/v1/responses',json={'stream':True})
        assert first.content==CREATED+DELTA and calls==['p2.example']
        second=await client.post('/r/worker/v1/responses',json={'stream':True})
        assert second.content==CREATED+COMPLETE and calls==['p2.example','p1.example']
        assert runtime.stats['reconnect_failovers']==1


async def test_ultra_balances_only_selected_cohort_and_rejects_cross_model():
    calls=[]
    async def handler(request):calls.append(request.url.host);return response(chunks=[b'{}'])
    config=settings();config=replace(config,providers=tuple(replace(p,deployment='same-model') for p in config.providers))
    async with client_for(handler,configuration=config) as (client,runtime):
        runtime.set_route({'id':'ultra','providers':['provider3','provider1'],'strategy':'balanced'})
        for _ in range(4):await client.post('/r/ultra/v1/responses',json={})
        assert calls==['p3.example','p1.example','p3.example','p1.example']
        runtime.states[1].provider=replace(runtime.states[1].provider,deployment='another-model')
        with pytest.raises(ValueError,match='same model'):runtime.set_route({'id':'invalid','providers':['provider1','provider2']})
        assert (await client.post('/r/missing/v1/responses',json={})).status_code==409


def plan():
    return {'summary':'Example','contract':'Two files','workers':[
        {'name':'A','task':'a','validation':'read','owned_paths':['a.txt']},
        {'name':'B','task':'b','validation':'read','owned_paths':['b.txt']} ]}


def test_experiment_isolation_merge_and_conflict_preserve_user_work(tmp_path):
    source=tmp_path/'project';source.mkdir();(source/'a.txt').write_text('old a\n');(source/'b.txt').write_text('old b\n')
    dest=tmp_path/'copies';prepare_copies(source,dest)
    (dest/'worker1/a.txt').write_text('new a\n');(dest/'worker2/b.txt').write_text('new b\n')
    assert (source/'a.txt').read_text()=='old a\n'
    assert sorted(merge_copies(source,dest,validate_plan(plan())))==['a.txt','b.txt']
    assert (source/'a.txt').read_text()=='new a\n' and (dest/'merge-backup/a.txt').read_text()=='old a\n'
    dest2=tmp_path/'copies2';prepare_copies(source,dest2)
    (dest2/'worker1/a.txt').write_text('AI changes\n');(source/'a.txt').write_text('user work\n')
    with pytest.raises(ValueError,match='poza zespołem'):merge_copies(source,dest2,plan())
    assert (source/'a.txt').read_text()=='user work\n'


def test_experiment_rejects_overlap_and_out_of_scope_edits_before_writing(tmp_path):
    p=plan();p['workers'][1]['owned_paths']=['A.txt/child']
    with pytest.raises(ValueError,match='sam plik'):validate_plan(p)
    source=tmp_path/'project';source.mkdir();(source/'a.txt').write_text('old\n');dest=tmp_path/'copies';prepare_copies(source,dest)
    (dest/'worker1/a.txt').write_text('valid change\n');(dest/'worker2/a.txt').write_text('invalid change\n')
    with pytest.raises(ValueError,match='poza swoim'):merge_copies(source,dest,plan())
    assert (source/'a.txt').read_text()=='old\n'


async def test_experiment_starts_two_real_protocol_workers_and_merges(tmp_path,vault):
    root=tmp_path/'project';root.mkdir();store=DashboardStore(tmp_path/'data/panel.sqlite3');project=store.add_project(str(root))
    runner=CodexRunner(store,tmp_path/'data','fixture-model',command=[sys.executable,str(Path(__file__).parent/'fixtures/fake_team_app_server.py')])
    routes=[]
    async def setup(route):routes.append(route)
    runner.route_setup=setup
    try:
        result=await runner.start_task(project['id'],'Two isolated files','max',options={'mode':'experimental','api_ids':['provider1','provider2','provider3']})
        task=runner.tasks[result['id']]
        await wait_for(lambda:task['state'] in {'completed','failed'},timeout=15)
        assert task['state']=='completed',task['messages']
        await wait_for(lambda:not runner._scanning)
        assert (root/'a.txt').read_text()=='first\nsecond\n' and (root/'b.txt').exists()
        assert task['tokens']['totalTokens']==300 and len(task['agents'])==3
        workers = {route['id'].rsplit('-', 1)[-1]: route for route in routes}
        assert workers['worker1']['providers']==['provider2','provider1','provider3'] and workers['worker2']['providers']==['provider3','provider1','provider2']
        assert task['files']==2 and task['added']==4
        assert task['experiment']['phase']=='completed'
    finally:await runner.close();store.close()


def test_skills_settings_apply_to_threads_without_global_config(tmp_path,vault):
    path=configuration(tmp_path)
    skill=tmp_path/'fixture-skill/SKILL.md';skill.parent.mkdir();skill.write_text('Fixture skill')
    def factory(store,directory,model):
        runner=CodexRunner(store,directory,model,command=[sys.executable,str(Path(__file__).parent/'fixtures/fake_codex_app_server.py')])
        async def inventory(cwd):return {'skills':[{'name':'fixture','description':'Fixture','path':str(skill),'enabled':True}]}
        runner.skills=inventory
        return runner
    app=create_app(data_dir=tmp_path/'data',config_path=path,runner_factory=factory,
                   transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))
    with TestClient(app,base_url='http://127.0.0.1:4001') as client:
        csrf=re.search('name="csrf-token" content="([^"]+)"',client.get('/ui/').text)[1]
        headers={'origin':'http://127.0.0.1:4001','x-panel-csrf':csrf}
        project=client.post('/ui/api/projects',json={'path':str(tmp_path)},headers=headers).json()
        saved=client.post('/ui/api/skills',json={'project_id':project['id'],'skills':[{'path':str(skill),'enabled':False}]},headers=headers)
        assert saved.status_code==200 and saved.json()['skills'][0]['enabled'] is False
        config=app.state.runner.task_config({'preferences':preferences(app.state.store)})
        assert config['skills.config']==[{'path':str(skill.parent),'enabled':False}]
        assert client.post('/ui/api/skills',json={'project_id':project['id'],'skills':[{'path':'C:/unknown/SKILL.md','enabled':False}]},headers=headers).status_code==400


async def test_proxy_reload_preserves_counters_and_refreshes_key_without_restart(tmp_path,vault):
    path=configuration(tmp_path)
    async def handler(request):return response(chunks=[b'{}'])
    async with client_for(handler,configuration=load_config(path),config_path=path) as (client,runtime):
        await client.post('/v1/responses',json={})
        state=runtime.states[0];attempts=state.attempts;identity=id(runtime)
        # client_for injects fixture secrets; changing this mapping simulates a vault update.
        runtime.secret_overrides={**runtime.secret_overrides,'provider1':'refreshed-key'}
        catalog=ProviderCatalog(path);catalog.change({'id':'provider1','label':'Reloaded provider'})
        result=await client.post('/admin/reload',json={})
        assert result.status_code==200 and id(runtime)==identity
        assert runtime.states[0] is state and state.attempts==attempts and state.key=='refreshed-key'
        assert state.provider.label=='Reloaded provider' and 'refreshed-key' not in result.text


def test_http_chat_and_run_history_survive_restart_with_idempotent_submission(tmp_path, vault):
    config = configuration(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = tmp_path / "data"

    def factory(store, data, model):
        return CodexRunner(store, data, model, command=[sys.executable, str(
            Path(__file__).parent / "fixtures/fake_codex_app_server.py")])

    def make_app():
        return create_app(data_dir=directory, config_path=config, runner_factory=factory,
                          transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))

    def session(client):
        csrf = re.search('name="csrf-token" content="([^"]+)"', client.get("/ui/").text)[1]
        return {"origin": "http://127.0.0.1:4001", "x-panel-csrf": csrf}

    app = make_app()
    with TestClient(app, base_url="http://127.0.0.1:4001") as client:
        headers = session(client)
        project = client.post("/ui/api/projects", json={"path": str(workspace)}, headers=headers).json()
        chat1 = client.post("/ui/api/chats", json={"project_id": project["id"]}, headers=headers).json()
        chat2 = client.post("/ui/api/chats", json={"project_id": project["id"]}, headers=headers).json()
        body = {"project_id": project["id"], "continue_task": chat1["id"], "prompt": "approve",
                "effort": "low", "client_request_id": "first-request", "permission_mode": "approval"}
        first = client.post("/ui/api/tasks", json=body, headers=headers)
        assert first.status_code == 202, first.text
        started = first.json()
        repeated = client.post("/ui/api/tasks", json=body, headers=headers).json()
        assert (repeated["id"], repeated["run_id"]) == (started["id"], started["run_id"])
        rejected = client.post("/ui/api/tasks", json={**body, "continue_task": chat2["id"],
                               "client_request_id": "second-request"}, headers=headers)
        assert rejected.status_code == 400
        listed = client.get(f'/ui/api/projects/{project["id"]}/chats').json()
        assert {chat["id"] for chat in listed["items"]} == {chat1["id"], chat2["id"]}
        assert all("messages" not in chat for chat in listed["items"])
        assert client.post(f'/ui/api/tasks/{chat1["id"]}/stop', json={}, headers=headers).status_code == 200
        deadline = time.monotonic() + 12
        while app.state.runner.tasks[chat1["id"]]["state"] in {"starting", "running", "stopping", "finalizing", "awaiting_input"}:
            assert time.monotonic() < deadline
            time.sleep(.03)
        history = client.get(f'/ui/api/projects/{project["id"]}/history').json()
        assert len(history["items"]) == 1 and history["items"][0]["id"] == started["run_id"]
        details = client.get(f'/ui/api/tasks/{chat1["id"]}/runs/{started["run_id"]}').json()
        assert any(message["role"] == "user" for message in details["messages"]["items"])
        assert details["finished_at"] is not None
        app.state.store.save_api_events(chat1["id"], started["run_id"], [{"id": "legacy-event", "kind": "legacy"}])
        app.state.telemetry.event("completed", "provider1", run_id=started["run_id"])
        observed = client.get(f'/ui/api/tasks/{chat1["id"]}/runs/{started["run_id"]}').json()["api_events"]
        assert len(observed["items"]) == 1 and observed["items"][0]["kind"] == "completed"
        after_event = client.get(f'/ui/api/tasks/{chat1["id"]}/runs/{started["run_id"]}', params={
            "api_events_cursor": str(observed["items"][0]["id"])}).json()["api_events"]
        assert after_event["items"] == [] and after_event["next_cursor"] is None
        for index in range(110):
            created = client.post("/ui/api/chats", json={"project_id": project["id"], "title": f"Chat {index}"}, headers=headers)
            assert created.status_code == 201
        assert client.get(f'/ui/api/projects/{project["id"]}/chats?limit=0').status_code == 400
        selected = client.get("/ui/api/state", params={"project_id": project["id"], "task_id": chat1["id"],
                                                      "run_id": started["run_id"]}).json()
        assert selected["schema_version"] == 2 and selected["task_id"] == chat1["id"]
        assert any(task["id"] == chat1["id"] for task in selected["tasks"])
    with TestClient(make_app(), base_url="http://127.0.0.1:4001") as client:
        session(client)
        result = client.get(f'/ui/api/projects/{project["id"]}/chats?limit=40').json()
        all_chats = list(result["items"])
        while result["next_cursor"]:
            result = client.get(f'/ui/api/projects/{project["id"]}/chats', params={
                "limit": 40, "cursor": result["next_cursor"]}).json()
            all_chats.extend(result["items"])
        assert len(all_chats) == 112 and len({chat["id"] for chat in all_chats}) == 112
        history = client.get(f'/ui/api/projects/{project["id"]}/history', params={"task_id": chat1["id"]}).json()
        assert history["items"][0]["id"] == started["run_id"]
