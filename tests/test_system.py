import asyncio
import json
import sys
import pytest
from jsonschema import ValidationError
from app.registry import Registry
from app.storage import Store
from app.workflow import run_workflow
from app.schemas import RunInput

@pytest.fixture
def store(tmp_path):return Store(tmp_path/'test.db')

def request(sid,**updates):
    return RunInput(session_id=sid,message='优先轻量化与安全',**updates).model_dump()

def test_demo_two_rounds_and_memory_isolation(store):
    a=store.session('A');b=store.session('B');reg=Registry()
    rid=store.create_run(a,request(a))
    asyncio.run(run_workflow(store,reg,rid,request(a),'demo'))
    data=store.run(rid)
    assert data['status']=='completed'
    assert len(data['result']['outputs'])==12
    assert {e['round'] for e in data['result']['outputs']}=={1,2}
    assert all(e['result']['status']=='not_implemented' for e in data['result']['outputs'])
    assert store.memory(b)=={}
    assert store.memory(a)['requirements']['goals']=={}
    assert store.memory(a)['requirements']['scenario']==''
    assert [e['level'] for e in data['events'] if e['type']=='disclosure'][:3]==['L1','L2','L3']
    newer=request(a,goals={'tensile':350,'yield_strength':200,'elongation':15})
    rid2=store.create_run(a,newer)
    asyncio.run(run_workflow(store,reg,rid2,newer,'demo'))
    assert store.memory(a)['requirements']['goals']['tensile']==350
    restarted=Store(store.path)
    assert restarted.memory(a)['last_summary']['run_id']==rid2
    restarted.clear_memory(a)
    assert restarted.history(a)==[] and restarted.memory(a)=={}
    assert restarted.run(rid)['result'] is not None

def test_schema_and_planned_contract():
    reg=Registry()
    assert len(reg.catalog())==6
    assert 'instructions' not in reg.catalog()[0]
    with pytest.raises(ValidationError):asyncio.run(reg.execute('performance',{}))
    with pytest.raises(ValueError):request('test',goals={'tensile':100,'yield_strength':200,'elongation':101})
    with pytest.raises(ValueError):request('../escape')

def test_mcp_real_subprocess():
    from app.mcp_bridge import call_tool
    spec={'mcp':{'type':'stdio','command':sys.executable,'args':['-m','app.mcp_server'],'tool':'interface_check'}}
    payload={k:v for k,v in request('test').items() if k in ('scenario','goals','message')}
    payload.update(round=1,previous=[],scenario='',goals={})
    result=asyncio.run(call_tool(spec,payload))
    assert result['data']['transport']=='mcp'
    assert result['status']=='not_implemented'

def test_workflow_failure_is_saved(store):
    class Broken(Registry):
        async def execute(self,*args):raise ValueError('fake')
    sid=store.session('error');req=request(sid);rid=store.create_run(sid,req)
    asyncio.run(run_workflow(store,Broken(),rid,req,'demo'))
    assert store.run(rid)['status']=='failed'
    assert store.run(rid)['events'][-1]['type']=='error'

def test_google_missing_key_is_explicit(store,monkeypatch):
    monkeypatch.delenv('GOOGLE_API_KEY',raising=False)
    sid=store.session('google');req=request(sid);rid=store.create_run(sid,req)
    asyncio.run(run_workflow(store,Registry(),rid,req,'google'))
    assert 'GOOGLE_API_KEY' in store.run(rid)['error']

def test_adk_runner_tools_and_response(monkeypatch):
    # 使用已安装的真实 ADK Agent/Session/RunConfig，替换唯一网络边界 Runner 事件流。
    from types import SimpleNamespace
    import google.adk.runners
    from app.model import GoogleBackend
    from google.genai import types
    monkeypatch.setenv('GOOGLE_API_KEY','offline-test-placeholder')
    class OfflineRunner:
        def __init__(self,agent,**kwargs):
            assert agent.name=='alloy_designer';assert len(agent.tools)==2
            self.agent=agent
        async def run_async(self,**kwargs):
            assert kwargs['run_config'].max_llm_calls==6
            tool1,tool2=self.agent.tools
            assert (await tool2('performance','{}'))['error']
            assert (await tool1('composition'))['error']
            await tool1('performance')
            assert (await tool2('performance','{}'))['status']=='not_implemented'
            yield SimpleNamespace(is_final_response=lambda:True,content=types.Content(parts=[types.Part(text='当前性能模型尚未实现')]))
    monkeypatch.setattr(google.adk.runners,'Runner',OfflineRunner)
    text=asyncio.run(GoogleBackend().stage({}, {'execution_payload':{},'skill_result':{'status':'not_implemented'}},Registry(),'performance',lambda *a,**kw:None))
    assert text=='当前性能模型尚未实现'

def test_api_roundtrip_and_invalid_input(tmp_path,monkeypatch):
    import app.main as main
    from app import config, planner
    async def planned(*args):
        return {'kind':'workflow','reply':'测试方案','planner':'测试模型','candidates':[{'id':'comprehensive','name':'离线测试方案','reason':'测试','steps':[s[0] for s in __import__('app.workflow',fromlist=['STAGES']).STAGES],'rounds':2}]}
    monkeypatch.setattr(planner,'generate_plan',planned)
    monkeypatch.setattr(config,"active_mode",lambda:"demo")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main,'store',Store(tmp_path/'api.db'))
    with TestClient(main.app) as client:
        assert client.get('/').status_code==200
        assert len(client.get('/api/skills').json())==6
        sid=client.post('/api/sessions',json={'title':'API test'}).json()['id']
        assert client.post('/api/runs',json=request('unknown')).status_code==404
        assert client.post('/api/runs',json={**request(sid),'rounds':1}).status_code==422
        result=client.post('/api/runs',json=request(sid))
        assert result.status_code==201
        pid=result.json()['id']
        assert client.get('/api/sessions/'+sid).json()['runs']==[]
        assert client.post('/api/plans/'+pid+'/confirm',json={'candidate_id':'missing'}).status_code==422
        result=client.post('/api/plans/'+pid+'/confirm',json={'candidate_id':'comprehensive'})
        assert result.status_code==202
        assert client.post('/api/plans/'+pid+'/confirm',json={'candidate_id':'comprehensive'}).status_code==409
        # 等待后台任务；TestClient 的事件循环继续调度。
        import time
        for _ in range(100):
            state=client.get('/api/runs/'+result.json()['id']).json()
            if state['status']!='running':break
            time.sleep(.01)
        assert state['status']=='completed'
        assert len(state['result']['outputs'])==12
        assert client.get('/api/sessions/'+sid).json()['memory']
        assert client.delete('/api/sessions/'+sid+'/memory').status_code==200
        assert client.get('/api/runs/missing').status_code==404
