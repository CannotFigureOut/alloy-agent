import asyncio
import json
import pytest
from app import config
from app.model import CompatibleBackend
from app.planner import generate_plan
from app.registry import Registry

@pytest.mark.parametrize('response',[
    {'kind':'chat','reply':'你好，请描述任务。','candidates':[]},
    {'kind':'clarification','reply':'请说明工况。','candidates':[]},
    {'kind':'workflow','reply':'建议先评估性能。','candidates':[{'name':'性能评估','reason':'仅评估所需能力，目前待实现','steps':['performance'],'rounds':1}]},
])
def test_model_decides_response_and_steps(monkeypatch,response):
    monkeypatch.setattr(config,'model_settings',config.ModelSettings('https://example.com/v1','secret','test'))
    catalog=Registry().catalog()
    async def complete(self,messages,max_tokens):
        context=json.loads(messages[1]['content'])
        assert context['agents']==catalog
        assert context['history']==[{'role':'user','content':'先前任务'}]
        return json.dumps(response,ensure_ascii=False)
    monkeypatch.setattr(CompatibleBackend,'complete',complete)
    result=asyncio.run(generate_plan('当前任务',catalog,[{'role':'user','content':'先前任务'}],{}))
    assert result['kind']==response['kind']
    if result['candidates']:assert result['candidates'][0]['steps']==['performance']


def test_unknown_skill_is_rejected(monkeypatch):
    monkeypatch.setattr(config,'model_settings',config.ModelSettings('https://example.com/v1','secret','test'))
    async def complete(*args,**kwargs):
        return json.dumps({'kind':'workflow','reply':'计划','candidates':[{'name':'方案','reason':'测试','steps':['unknown'],'rounds':1}]})
    monkeypatch.setattr(CompatibleBackend,'complete',complete)
    with pytest.raises(ValueError):asyncio.run(generate_plan('任务',Registry().catalog(),[],{}))
