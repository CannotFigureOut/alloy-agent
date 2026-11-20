import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .config import ROOT, DB, MODE, MODEL
from .schemas import RunInput, NewSession, PlanConfirmation, ModelConfiguration
from .storage import Store
from .registry import Registry
from .workflow import run_workflow

store=Store(DB)
registry=Registry()
tasks={}
@asynccontextmanager
async def lifespan(app):
    yield
    for task in tasks.values():task.cancel()
    await asyncio.gather(*tasks.values(),return_exceptions=True)

app=FastAPI(title='AlloyPilot · 铝合金设计智能体',version='0.1.0',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=ROOT/'app/static'),name='static')
@app.get('/')
def home():return FileResponse(ROOT/'app/static/index.html')
@app.get('/api/config')
def config():
    from . import config as settings
    mode=settings.active_mode()
    return {'mode':mode,'model':settings.model_settings.model_name if mode=='openai' else MODEL if mode=='google' else '本地流程演示','domain':'新能源汽车用铝合金'}
@app.get('/api/skills')
def skills():return registry.catalog()
@app.post('/api/sessions',status_code=201)
def session(body:NewSession):return {'id':store.session(body.title)}
@app.get('/api/sessions')
def sessions():return store.sessions()
def check(sid):
    if not store.exists(sid):raise HTTPException(404,'会话不存在')
@app.get('/api/sessions/{sid}')
def session_detail(sid:str):
    check(sid)
    return {'memory':store.memory(sid),'messages':store.history(sid,100),'runs':store.runs(sid),'plans':store.pending_plans(sid)}
@app.delete('/api/sessions/{sid}/memory')
def clear(sid:str):
    check(sid)
    if sid in tasks:raise HTTPException(409,'请等待当前会话运行完成')
    store.clear_memory(sid)
    return {'ok':True}
@app.post('/api/plans',status_code=201)
@app.post('/api/runs',status_code=201)
async def plan(body:RunInput):
    check(body.session_id)
    if body.session_id in tasks:raise HTTPException(409,'当前会话已有任务运行')
    catalog=sorted(registry.catalog(),key=lambda x:x.get('stage',0))
    from .planner import generate_plan
    try:
        generated=await generate_plan(body.message,catalog,store.history(body.session_id,16),store.memory(body.session_id))
    except RuntimeError as exc:
        if str(exc).startswith('请先在模型设置'):raise HTTPException(503,str(exc)) from None
        raise HTTPException(502,'模型规划失败，请检查模型配置后重试') from None
    except Exception:
        raise HTTPException(502,'模型规划失败或返回的方案无效，请重试或检查模型配置') from None
    payload={'request':body.model_dump(),'catalog':catalog,**generated}
    pid=store.save_plan(body.session_id,payload) if generated['kind']=='workflow' else None
    store.message(body.session_id,'user',body.message)
    store.message(body.session_id,'assistant',generated['reply'])
    return {'id':pid,**payload}

@app.get('/api/plans/{pid}')
def plan_detail(pid:str):
    data=store.plan(pid)
    if data is None:raise HTTPException(404,'方案不存在')
    return data

@app.post('/api/plans/{pid}/confirm',status_code=202)
async def confirm(pid:str,body:PlanConfirmation):
    data=store.plan(pid)
    if data is None:raise HTTPException(404,'方案不存在')
    sid=data['session_id']
    if sid in tasks:raise HTTPException(409,'当前会话已有任务运行')
    if len(tasks)>=4:raise HTTPException(429,'最多同时运行四个会话')
    if __import__('app.config',fromlist=['active_mode']).active_mode() not in ('demo','google','openai'):raise HTTPException(503,'AGENT_MODE 仅支持 demo/google')
    candidate=next((c for c in data['payload']['candidates'] if c['id']==body.candidate_id),None)
    if candidate is None:raise HTTPException(422,'候选方案不存在')
    if not store.claim_plan(pid):raise HTTPException(409,'该方案已经确认，不能重复执行')
    payload={**data['payload']['request'],'steps':candidate['steps'],'rounds':candidate['rounds'],'plan_id':pid}
    rid=store.create_run(sid,payload)
    task=asyncio.create_task(run_workflow(store,registry,rid,payload))
    tasks[sid]=task
    task.add_done_callback(lambda _:tasks.pop(sid,None))
    return {'id':rid}
@app.get('/api/runs/{rid}')
def run_detail(rid:str):
    data=store.run(rid)
    if data is None:raise HTTPException(404,'运行不存在')
    return data


@app.get('/api/model-settings')
def get_model_settings():
    from . import config
    s=config.model_settings
    return {'base_url':s.base_url,'model_name':s.model_name,'has_api_key':bool(s.api_key),'mode':config.active_mode()}

def resolved_settings(body):
    from .config import model_settings, ModelSettings
    key=body.api_key or model_settings.api_key
    if not key:raise HTTPException(422,'请填写 API Key')
    return ModelSettings(body.base_url,key,body.model_name)

@app.post('/api/model-settings/test')
async def test_model_settings(body:ModelConfiguration):
    from .model import CompatibleBackend
    settings=resolved_settings(body)
    try:
        await CompatibleBackend(settings).complete([{'role':'user','content':'请回复 OK。'}],32)
        return {'ok':True,'message':'模型连通性测试成功'}
    except Exception as exc:
        # 不返回服务商响应、地址或异常文本，防止回显密钥。
        import urllib.error
        status=exc.code if isinstance(exc,urllib.error.HTTPError) else None
        raise HTTPException(502,f'模型测试失败'+(f'（HTTP {status}）' if status else '，请检查地址、模型名称和网络')) from None

@app.post('/api/model-settings')
def update_model_settings(body:ModelConfiguration):
    from . import config
    settings=resolved_settings(body)
    try:config.save_model_settings(settings)
    except OSError:raise HTTPException(500,'无法保存 .env，请检查文件写入权限') from None
    return {**get_model_settings(),'message':'配置已保存并立即生效；正在执行的任务继续使用原配置'}
