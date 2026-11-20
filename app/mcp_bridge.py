import json
import os
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

async def call_tool(spec,payload):
    """独立 MCP 客户端：仅连接管理员 manifest 预配置的服务。"""
    cfg=spec['mcp']
    async with AsyncExitStack() as stack:
        if cfg['type']=='stdio':
            params=StdioServerParameters(command=cfg['command'],args=cfg.get('args',[]),env={**os.environ,**cfg.get('env',{})})
            read,write=await stack.enter_async_context(stdio_client(params))
        elif cfg['type']=='streamable_http':
            read,write,_=await stack.enter_async_context(streamablehttp_client(cfg['url']))
        else:raise ValueError('MCP transport 不支持')
        session=await stack.enter_async_context(ClientSession(read,write))
        await session.initialize()
        response=await session.call_tool(cfg['tool'],payload)
        if response.isError:raise RuntimeError('MCP 工具返回错误')
        structured=getattr(response,'structuredContent',None)
        if structured is not None:return structured
        text=''.join(c.text for c in response.content if c.type=='text')
        return json.loads(text)
