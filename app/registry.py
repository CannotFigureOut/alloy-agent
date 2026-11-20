import importlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from .config import ROOT

class Registry:
    """L1 只读 manifest；L2 读取指令；L3 执行 JSON Schema 校验后的白名单插件。"""
    def __init__(self, root=None):
        self.root=Path(root or ROOT / 'skills').resolve()
        self.items={}
        for path in sorted(self.root.glob('*/manifest.json')):
            data=json.loads(path.read_text(encoding='utf-8'))
            Draft202012Validator.check_schema(data['input_schema'])
            Draft202012Validator.check_schema(data['output_schema'])
            if data['id'] in self.items:raise ValueError('重复 Skill ID')
            self.items[data['id']]={**data,'_dir':path.parent}
    def catalog(self):
        return [{k:v for k,v in s.items() if k in ['id','name','description','stage','status','transport','version']} for s in self.items.values()]
    def detail(self,skill_id):
        s=self.items[skill_id]
        path=(s['_dir']/s['instructions']).resolve()
        if not path.is_relative_to(s['_dir']):raise ValueError('指令路径越界')
        return {'id':skill_id,'instructions':path.read_text(encoding='utf-8'), 'input_schema':s['input_schema'],'output_schema':s['output_schema']}
    async def execute(self,skill_id,payload):
        s=self.items[skill_id]
        Draft202012Validator(s['input_schema']).validate(payload)
        if s['status']=='planned':
            result={'status':'not_implemented','skill_id':skill_id,'data':{},'evidence':[], 'warnings':['功能尚未实现；无材料预测值、文献值或实测值。']}
        elif s['transport']=='python':
            module,name=s['entrypoint'].split(':')
            # 只有服务器运维者能安装插件；请求不能传入模块或脚本路径。
            handler=getattr(importlib.import_module(module),name)
            result=await handler(payload)
        elif s['transport']=='mcp':
            from .mcp_bridge import call_tool
            result=await call_tool(s,payload)
        else:raise ValueError('未知插件 transport')
        Draft202012Validator(s['output_schema']).validate(result)
        return result
