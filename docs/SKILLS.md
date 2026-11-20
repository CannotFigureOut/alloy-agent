# Skill 格式与开发接口

这是项目自己的 Skill 插件协议，不等同于 ADK 的内建 SkillService。SKILL.md 负责自然语言操作说明，manifest.json 是执行与校验契约。

## 最小目录

```text
skills/performance/
  manifest.json
  SKILL.md
  references/          可选，后续手动按需加载；当前无自动资源读取器
```

## manifest.json 字段

| 字段 | 含义 |
|---|---|
| id / name / description | 唯一 ID、名称、L1 能力摘要 |
| version / stage | 插件版本，工作流阶段 |
| status | planned 返回未实现；ready 才加载实际处理器 |
| transport | python 或 mcp |
| entrypoint | python 模式 `包.模块:异步函数` |
| instructions | 本插件目录内的 SKILL.md 相对路径 |
| input_schema / output_schema | JSON Schema 2020-12 |
| mcp | mcp 模式的服务与工具配置 |

六个完整示例在 skills/*/manifest.json。新增 ID 可被目录发现；要让新 ID 加入调度，还需修改 app/workflow.py 的 STAGES。当前固定六阶段，不会自动将任意新插件插入流程。插件更新后重启应用。

## SKILL.md 建议格式

```markdown
# 能力名称
## 触发
## 目标
## 输入
## 步骤
## 输出与来源
## 缺失条件与失败处理
```

正文只在阶段触发时读取，不应存 API 密钥。读取路径必须位于当前 Skill 目录中。

## Python 插件

1. 新建 `app/my_performance.py`，实现 `async def handle(payload: dict) -> dict`。
2. manifest 改为 `status: ready`、`transport: python`、`entrypoint: app.my_performance:handle`。
3. 按现有 input_schema 接收参数；按 output_schema 返回结构化结果。

```python
async def handle(payload: dict) -> dict:
    # 用可信模型或数据库替换此处。没有模型就保留未实现。
    return {
        'status': 'not_implemented',
        'skill_id': 'performance',
        'data': {},
        'evidence': [],
        'warnings': ['尚未接入预测模型']
    }
```

状态为 ok 也必须提供真实来源；基础框架只校验结构，不能自动验证来源真伪。领域开发者还应校验元素总质量分数、输入适用域、候选数量、来源和单位。运行耗时较长的同步计算应使用 asyncio.to_thread 或独立任务服务，避免阻塞事件循环。

## MCP stdio 接入

把 performance manifest 改为 ready/mcp，增加：

```json
{
  "mcp": {
    "type": "stdio",
    "command": "/absolute/path/to/python",
    "args": ["-m", "app.mcp_server"],
    "tool": "interface_check"
  }
}
```

Windows command 使用实际 Python 绝对路径，JSON 中反斜杠需转义。命令必须在项目根目录下运行，或让插件模块已安装。上述 MCP 演示只返回“连接正常、模型未接入”，不会预测材料性能。

`mcp.type=streamable_http` 时使用 `url` 和 `tool`；HTTP 通道代码已预留，本次未测试远程端点。API token 可从服务端配置扩展 headers 处理，当前 HTTP 示例不实现鉴权。

MCP 服务的工具参数必须接受 scenario、goals、message、round、previous 五个字段，或者由开发者扩展适配器。结果必须是统一 output_schema 的 JSON。优先读取 MCP structuredContent，回退读取 text block 的 JSON。不要向 stdio stdout 打印普通日志。

## 证据类型

`evidence` 的 kind 区分 target / prediction / literature / measurement，source 是模型版本、文献标识或实验记录。单位字段建议明确 MPa、%、wt.%、°C 等，组织/工艺状态需要一起记录。空 evidence 表示没有证据，不表示通过验证。
