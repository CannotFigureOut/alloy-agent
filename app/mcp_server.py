"""运行 python -m app.mcp_server；提供基础连通性工具，不是材料预测模型。"""
from mcp.server.fastmcp import FastMCP
mcp=FastMCP('alloy-interface-demo')
@mcp.tool()
def interface_check(scenario: str, goals: dict, message: str, round: int, previous: list) -> dict:
    """验证 MCP 连接与 Skill 输入输出契约。"""
    return {'status':'not_implemented','skill_id':'performance','data':{'transport':'mcp','round':round},
            'evidence':[],'warnings':['MCP 连接正常；性能模型未接入']}
if __name__=='__main__':mcp.run(transport='stdio')
