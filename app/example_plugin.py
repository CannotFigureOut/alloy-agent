async def handle(payload: dict) -> dict:
    """仅演示 Python 插件契约；复制后实现领域逻辑并修改 manifest。"""
    return {'status':'not_implemented','skill_id':'example','data':{},'evidence':[],
            'warnings':['插件契约示例，不包含领域计算']}
