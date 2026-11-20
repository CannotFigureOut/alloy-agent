# AlloyPilot · 铝合金设计智能体框架

面向新能源汽车铝合金设计的对话式智能体工作台。用户用自然语言描述场景、性能目标和约束，智能体读取全部已注册 Skill 的介绍，通过大模型理解任务并生成候选工作流。用户确认方案后，后端按步骤执行并显示进度。

目前已实现前后端交互、模型设置、语义规划、Python/MCP 插件接口和 SQLite 持久化。六个领域 Skill 仍为占位接口，返回 `not_implemented`；框架运行完成不代表完成材料预测、优化或科学验证。

## 快速启动

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，确保终端可执行 `uv --version`。项目使用 Python 3.12，启动脚本通过 uv 准备解释器、创建或同步 `.venv`，并按 `uv.lock` 安装运行依赖。

macOS / Linux：

```bash
cd alloy-agent
bash run.sh
```

Windows PowerShell：

```powershell
cd alloy-agent
.\run.ps1
```

脚本会在 `.env` 不存在时复制 `.env.example`，保留已有配置。无需手动激活虚拟环境，也不需要 Node.js。依赖同步失败时脚本停止；已有环境不兼容时，应先备份 `.venv` 再重新运行脚本。

启动后打开 http://127.0.0.1:8000 ，API 文档位于 http://127.0.0.1:8000/docs 。脚本默认监听本机 8000 端口，使用一个 Uvicorn worker。

也可手动执行：

```bash
uv sync --frozen --no-dev --python 3.12
# 首次运行时复制 .env.example 为 .env，不要覆盖已有配置
uv run --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`pyproject.toml` 声明项目依赖，`uv.lock` 固定解析结果。启动脚本不会更新锁文件；修改依赖后需运行 `uv lock` 更新锁文件。项目统一使用 uv 管理依赖。

## 模型设置

首次使用时点击顶部「模型设置」，填写支持 OpenAI Chat Completions 兼容接口的服务配置：

| 字段 | 含义 |
|---|---|
| `base_url` | API 基础地址，通常以 `/v1` 结尾，后端会追加 `/chat/completions` |
| `api_key` | 服务商密钥 |
| `model_name` | 服务商提供的模型名称 |

「测试连通性」使用当前表单发送一条简短生成请求，不保存配置。「保存并应用」将配置原子写入后端 `.env`，新任务立即使用新配置，无需重启。密钥留空时保留已保存的密钥，后端不会将密钥返回前端。运行中的工作流继续使用启动时的模型配置。

也可在启动前编辑 `.env`：

```dotenv
AGENT_MODE=openai
MODEL_BASE_URL=https://your-provider.example/v1
MODEL_API_KEY=your-api-key
MODEL_NAME=your-model-name
```

自定义模型同时用于对话理解、工作流规划和阶段结果分析。规划需要完整的上述配置；未配置、模型请求失败或返回无效方案时会提示错误，不会回退为固定流程。

代码保留了 Google ADK / Gemini 阶段执行适配，使用 `GOOGLE_API_KEY` 和 `MODEL` 配置；它并未独立提供当前规划接口所需的模型配置。默认 `demo` 模式仅保留本地阶段演示能力，不能替代大模型语义规划。

## 对话与执行流程

1. 用户在主对话框用自然语言描述任务；不再提供独立的场景和性能目标表单，也不自动补入默认指标。
2. 后端读取全部已注册智能体介绍，将当前消息、历史对话和记忆传给大模型。
3. 模型判断输入意图：普通交流直接回复；信息不足时提出澄清问题；可规划的任务生成 1–4 个候选方案，自主选择步骤、顺序及 1–3 轮迭代。
4. 后端校验方案格式与 Skill ID，保存有效候选；前端展示方案说明、步骤及确认按钮。
5. 用户确认后，服务器执行保存的候选流程，拒绝重复确认，逐步展示运行状态与结果。

右侧列出 Skill 名称、介绍和实现状态。待确认方案、会话消息、结构化记忆及运行轨迹保存在 SQLite 中，刷新可恢复待确认方案。当前前端不会自动继续轮询刷新前已启动的运行，可通过运行 API 查询。

## 领域插件

| Skill ID | 能力 | 当前状态 |
|---|---|---|
| `requirements` | 服役需求建模 | 指令和输入输出 Schema 已预留 |
| `composition` | 候选成分与元素约束 | 领域功能待实现 |
| `microstructure` | 组织、相与表征方案 | 领域功能待实现 |
| `performance` | 性能评估与证据来源 | 领域功能待实现 |
| `process` | 制备、工艺与质控 | 领域功能待实现 |
| `decision` | 多目标决策与迭代 | 领域功能待实现 |

插件通过 `manifest.json` 注册，按需加载 `SKILL.md`，执行前后校验 JSON Schema。支持服务端配置的 Python 入口和 MCP 工具；请求不能任意指定模块或脚本。模型分析须区分目标、预测、文献与实测，不得将占位结果描述为真实材料验证。

## 目录

```text
app/main.py          API、会话管理、方案确认与任务调度
app/planner.py       大模型语义理解、动态规划与方案校验
app/model.py         OpenAI 兼容接口、Google ADK 阶段适配
app/config.py        配置加载、.env 保存与模型热更新
app/workflow.py      所选步骤执行、迭代、事件与错误处理
app/registry.py      Skill 注册、分层加载与 Schema 校验
app/mcp_bridge.py    MCP stdio / Streamable HTTP 客户端
app/mcp_server.py    MCP 接口演示服务
app/storage.py       SQLite 会话、消息、方案、记忆与轨迹
app/static/          对话界面、模型设置弹窗与插件侧栏
skills/              六个 Skill 的 manifest.json 与 SKILL.md
tests/               工作流、模型设置与语义规划测试
```

## 主要 API

| 接口 | 用途 |
|---|---|
| `GET /api/skills` | 获取插件介绍 |
| `POST /api/sessions` | 创建会话 |
| `GET /api/sessions/{sid}` | 查询消息、记忆、运行和待确认方案 |
| `POST /api/plans` | 处理消息，返回回复、澄清或候选工作流 |
| `POST /api/plans/{pid}/confirm` | 确认候选并启动运行 |
| `GET /api/runs/{rid}` | 查询运行状态、事件和结果 |
| `GET /api/model-settings` | 获取模型配置，不含密钥 |
| `POST /api/model-settings/test` | 测试表单中的模型配置 |
| `POST /api/model-settings` | 保存配置并立即应用 |

`POST /api/runs` 是消息规划入口的兼容路由，返回 201，不直接执行工作流。

## 验证

```bash
uv sync --frozen --python 3.12
uv run --no-sync python -m pytest -q
node --check app/static/app.js
```

Node.js 仅用于可选的前端语法检查。测试包含模拟模型响应的语义规划、未知 Skill 拒绝、确认前不执行、重复确认拒绝、模型配置保存和密钥隐藏，以及真实 MCP 子进程通信。离线测试通过不代表真实服务商连通性通过，请使用设置弹窗测试实际配置。

## 运行边界

当前为本机单用户原型，无身份认证，模型设置接口可以修改服务端 `.env`，应仅在可信本地环境使用。工作流单会话互斥、最多四个会话同时执行，任务由当前进程管理。服务重启后遗留运行标记为失败，不支持从中断节点恢复。

默认数据库为 `data/alloy.db`，可通过 `DATABASE_PATH` 配置。清空记忆删除当前会话消息与记忆，运行历史和方案仍保留。模型通过界面保存后即时生效；修改插件、其他环境配置或应用代码后需重启服务。
