import asyncio
from .config import MODE, TIMEOUT

STAGES=[('requirements','需求建模'),('composition','成分设计'),('microstructure','组织与相设计'),('performance','性能评估'),('process','制备与工艺'),('decision','多目标决策')]

async def run_workflow(store,registry,rid,request,mode=None):
    from . import config
    mode=mode or config.active_mode()
    sid=request['session_id']
    def emit(kind,**data):store.event(rid,{'type':kind,**data})
    try:
        previous=store.memory(sid)
        history=store.history(sid,8)
        # 自然语言为任务来源，未指定指标不注入默认值。
        request={**request,'scenario':request.get('scenario') or '', 'goals':request.get('goals') or {}}
        store.remember(sid,'requirements',{'message':request['message'],'scenario':request['scenario'],'goals':request['goals']})
        if not request.get('plan_id'):store.message(sid,'user',request['message'])
        emit('memory',keys=list(previous),history_count=len(history))
        emit('disclosure',level='L1',catalog=registry.catalog())
        outputs=[]
        from .model import GoogleBackend
        from .model import CompatibleBackend
        backend=CompatibleBackend(config.model_settings) if mode=='openai' else GoogleBackend() if mode=='google' else None
        for round_no in range(1,request['rounds']+1):
            for skill_id in request.get('steps',[s[0] for s in STAGES]):
                title=registry.items[skill_id]['name']
                emit('stage',status='running',round=round_no,skill_id=skill_id,title=title)
                detail=registry.detail(skill_id)
                emit('disclosure',level='L2',skill_id=skill_id,round=round_no)
                payload={'scenario':request['scenario'],'goals':request['goals'],'message':request['message'],'round':round_no,'previous':outputs[-6:]}
                emit('disclosure',level='L3',skill_id=skill_id,round=round_no)
                result=await asyncio.wait_for(registry.execute(skill_id,payload),TIMEOUT)
                context={'memory':previous,'history':history,'current_round':round_no,'recent_outputs':outputs[-6:],'skill_result':result,'execution_payload':payload}
                if backend:
                    note=await backend.stage(request,context,registry,skill_id,emit)
                else:
                    note=f'第 {round_no} 轮 · {title}：已加载接口与指令；领域功能待接入。'
                    if skill_id=='requirements' and request['goals']:
                        g=request['goals'];note+=f"目标：抗拉 >{g['tensile']} MPa、屈服 >{g['yield_strength']} MPa、伸长率 >{g['elongation']}%。"
                    if round_no>1:note+=' 本轮携带上一轮结果；尚无预测数据，不能认定性能改进。'
                output={'round':round_no,'skill_id':skill_id,'title':title,'result':result,'note':note}
                outputs.append(output)
                emit('stage',status='completed',**output)
                await asyncio.sleep(0)
        pending=any(o['result']['status']=='not_implemented' for o in outputs)
        conclusion='工作流执行结束；部分领域 Skill 尚未实现，不能判定材料达标。' if pending else '已完成所选工作流，请结合各阶段证据审查结果。'
        summary={'mode':mode,'requirements':{'message':request['message'],'scenario':request['scenario'],'goals':request['goals']},'outputs':outputs,'conclusion':conclusion,'next_steps':['实现至少三个候选成分及元素约束校验','接入有来源的组织、性能与工艺模型','用两轮真实候选和评估结果进行透明排序']}
        store.remember(sid,'last_summary',{'run_id':rid,'mode':mode,'scenario':request['scenario'],'rounds':request['rounds'],'conclusion':summary['conclusion']})
        store.message(sid,'assistant',summary['conclusion'])
        store.finish(rid,summary)
        emit('done',status='completed')
    except asyncio.CancelledError:
        store.finish(rid,error='服务关闭，运行中断')
        raise
    except Exception as exc:
        # 对客户端不回显包含密钥或请求头的第三方异常。
        import logging
        logging.getLogger(__name__).error('Run %s failed: %s',rid,type(exc).__name__)
        message=str(exc) if isinstance(exc,RuntimeError) and str(exc).startswith('Google 模式需要') else f'运行失败（{type(exc).__name__}），请检查服务日志、模型配置或 Skill 输入输出'
        store.finish(rid,error=message)
        emit('error',message=message)
