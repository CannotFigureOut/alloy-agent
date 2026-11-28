---
name: materials_kg_rag
description: 从材料科学 PDF 文献（新能源汽车用铝合金等）中解析原文、抽取三元组、构建 Neo4j 知识图谱，并基于图谱进行可溯源的 RAG 问答。当用户上传 PDF 文献、要求文献入库或人工校对、询问材料牌号与力学性能（如 6082 抗拉强度、T6 时效工艺参数）、或需要追溯某个数值来自哪篇文献哪句话时，应使用本 Skill。关键词：PDF 转 Markdown、知识图谱、材料性能、铝合金、文献溯源、RAG 问答、Neo4j。
allowed-tools: Python
---

# 材料文献智能解析与知识构建 Skill

## 目的

把材料科学文献转化为结构化知识底座，并在问答时保留"数据 ← 知识陈述 ← 文献原文"的完整证据链。本 Skill 是一个**可被 Agent 框架调用的能力包**，不是网页应用，也不需要改宿主框架的代码。

工作流：

```
用户上传 PDF
  → parse_pdf_to_markdown()      转 Markdown（可人工校对）
  → build_knowledge_graph()     LLM 抽取三元组 → 写入 Neo4j
  → rag_query()                 图谱召回 → 拼上下文 → LLM 回答 + 文献来源
```

## 何时触发

- 用户提供 PDF 并要求阅读、解析、入库、提取表格 / 性能数据。
- 用户询问某材料牌号的性能、工艺或数值（"6082-T6 的抗拉强度是多少"）。
- 用户要求追溯结论出处（"这个数据是哪篇论文里的"）。
- 用户要求构建 / 更新材料知识图谱或历史设计数据库。

## 前置条件

1. Python 3.10+，已安装 `requirements.txt` 中的核心依赖（neo4j、openai、pymupdf；推荐另装 pymupdf4llm）。
2. 一个可连接的 Neo4j 实例（本地 Neo4j Desktop 即可）。
3. 一个 OpenAI 兼容的 LLM API Key（OpenAI / DeepSeek / 其他均可）。
4. 环境变量已配置（见 `.env.example`）；宿主 Agent 可用 `config.get_settings().public_dict()` 检查状态。

可选：
- 配置 `PDF2MD_SKILL_DIR` 指向 `pdf2md-agent-skill` 仓库 → 启用方案 A（公式 LaTeX、双栏、扫描页 OCR、图片导出）。
- 未安装 `pymupdf4llm` 时自动降级到纯 PyMuPDF 增强方案 C，**同样能识别表格和图片**，无需下载任何模型。

## 标准工作流

1. **解析**：调用 `parse_pdf_to_markdown(pdf_path, language="auto")`，得到 Markdown 字符串并存入 `paper_library/<paper_id>.md`；PDF 内嵌图片导出到 `paper_library/<paper_id>_assets/`，Markdown 用相对路径引用，两者可一起上传 GitHub。
2. **校对（可选但推荐）**：把 Markdown 交给用户确认 / 修改；修改后调用 `save_paper(paper_id, text)` 覆盖入库。材料文献的表格与性能数值尤其需要校对。
3. **建图**：调用 `build_knowledge_graph(markdown_text, paper_id, paper_title)`。函数内部完成分块、LLM 抽取、索引创建、幂等写入，并让每条 `Knowledge` 通过 `REPORTS` 回溯到 `Literature`。
4. **问答**：调用 `rag_query(question, top_k=5)`，返回带 `[来源 N]` 标注的答案与文献清单。

首次运行前（或新增索引后）可显式调用 `kg_builder.ensure_indexes()`；`build_knowledge_graph` 默认会自动执行。

## 工具函数（Agent 可调用）

所有函数均位于本目录的 Python 模块中，导入方式与宿主框架一致：

目录名 `materials-kg-skill` 含连字符，不能直接作为 Python 包名。两种挂载方式任选其一，各模块已做双模式导入兼容：

```python
# 方式 1（推荐）：把本目录加入 sys.path，按顶层模块导入
from pdf_parser import parse_pdf_to_markdown
from kg_builder import build_knowledge_graph
from rag_query import rag_query

# 方式 2：把文件夹重命名为 materials_kg_skill（下划线）后按包导入
from materials_kg_skill import get_tools, TOOL_SPECS
```

### `parse_pdf_to_markdown(pdf_path, language="auto", paper_id=None, prefer_external_skill=True, save_to_library=True, backend=None, write_images=None, table_strategy=None, dpi=None) -> str`

将 PDF（或 .md/.txt）转换为 Markdown 字符串并存入论文库，同时导出图片。
- `language`: `auto` / `zh` / `en`，`auto` 读取前两页自动判断。
- `backend`: `auto` / `pdf2md_skill` / `pymupdf4llm` / `pymupdf_enhanced`；`auto` 自动逐级降级，任一后端失败都不中断。
- `write_images`: 是否导出 PDF 内嵌图片（默认取 `PDF_WRITE_IMAGES`）。图片存 `<paper_id>_assets/`，Markdown 生成相对引用。
- `table_strategy`: `lines_strict`（默认）/ `lines` / `text` / `explicit`。三线表由 caption 锚定的词对齐重建自动处理，一般无需改动。
- `dpi`: 图片导出分辨率，默认取 `PDF_IMAGE_DPI`（150）。
- 返回：Markdown 全文。异常：`FileNotFoundError` / `RuntimeError`。

### `build_knowledge_graph(markdown_text, paper_id, paper_title=None, source_file=None, extractor=None, ensure_indices=True) -> dict`

从 Markdown 抽取材料知识并写入 Neo4j。
- `extractor`: 可选自定义抽取函数 `(chunk, paper_id, title, chunk_index) -> dict`，用于替换默认 LLM 抽取器（离线测试、私有模型接入）。
- 写入是幂等的（`MERGE` + 稳定 uid），同一文献重复入库不会产生重复知识节点。
- 返回：`{"paper_id", "title", "chunks", "knowledge_nodes", "entities", "errors"}`。

### `rag_query(question, top_k=None, include_context=False) -> dict`

基于图谱回答问题。
- 返回：`{"question", "answer", "sources", "keywords", "retrieved", "method", optional "context"}`。
- `sources` 中每项含 `index` / `title` / `paper_id`，与答案里的 `[来源 N]` 对应。
- 无相关知识时返回明确的"根据当前文献库无法确定"，不编造答案。
- 只要答案文本时可用 `rag_query_text(question)`。

### 其他辅助函数

| 函数 | 模块 | 说明 |
|---|---|---|
| `build_from_pdf(pdf_path, ...)` | kg_builder | 一步完成 PDF → Markdown → 图谱 |
| `build_from_pdf_graphrag(...)` | kg_builder | 可选：Neo4j 官方 `SimpleKGPipeline` 高阶后端 |
| `retrieve_context(question, top_k)` | rag_query | 只做检索，返回 snippets 与格式化上下文 |
| `ensure_indexes()` | kg_builder | 建立全文索引与唯一约束 |
| `graph_stats()` | rag_query | 各类型节点数量 |
| `list_paper_library()` / `load_paper()` / `save_paper()` | pdf_parser | 论文库管理（条目含 `images` 图片计数） |
| `list_paper_images(paper_id)` | pdf_parser | 列出该文献导出的图片文件路径，供人工校对 |
| `get_tools()` / `TOOL_SPECS` | `__init__` | 供框架自动注册工具 / 生成 JSON Schema |

## 本体（Ontology）

节点：`Material`（牌号）、`Property`（性能名称）、`Value`（带单位数值）、`Process`（工艺）、`Literature`（文献）、`Knowledge`（知识陈述）。

| 关系 | 含义 |
|---|---|
| `(Literature)-[:REPORTS]->(Knowledge)` | 文献报道了该条知识（证据链关键） |
| `(Knowledge)-[:ABOUT_MATERIAL]->(Material)` | 知识涉及的材料 |
| `(Knowledge)-[:HAS_PROPERTY]->(Property)` | 知识涉及的性能 |
| `(Property)-[:HAS_VALUE]->(Value)` | 性能对应的数值 |
| `(Material)-[:PROCESSED_BY]->(Process)` | 材料经过的工艺 |

`Knowledge` 节点保存 `statement`、`evidence`（原文句子）、`paper_id`、`chunk_id`、`uid`；因此从任一性能节点都能反查到具体文献与原句。

## 配置（环境变量）

| 变量 | 说明 | 默认 |
|---|---|---|
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | Neo4j 连接 | `bolt://localhost:7687` / `neo4j` / 空 |
| `NEO4J_DATABASE` | 数据库名，留空用默认 | 空 |
| `LLM_API_KEY` | LLM 密钥（也可 `OPENAI_API_KEY`） | 空 |
| `LLM_BASE_URL` | 兼容接口地址，如 `https://api.deepseek.com` | OpenAI 默认 |
| `LLM_MODEL` | 对话模型 | `gpt-4o-mini` |
| `EMBEDDING_MODEL` | 仅 GraphRAG 后端需要 | `text-embedding-3-small` |
| `PDF2MD_SKILL_DIR` | 方案 A 的仓库路径 | 空 |
| `PDF_BACKEND` | `auto` / `pdf2md_skill` / `pymupdf4llm` / `pymupdf_enhanced` | `auto` |
| `PDF_TABLE_STRATEGY` | 表格检测策略 `lines_strict` / `lines` / `text` / `explicit` | `lines_strict` |
| `PDF_WRITE_IMAGES` | 是否导出 PDF 图片 | `true` |
| `PDF_IMAGE_DPI` | 图片导出分辨率 | `150` |
| `PAPER_LIBRARY_DIR` | 论文库目录 | `./paper_library` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` / `TOP_K` | 分块与召回参数 | `1800` / `200` / `5` |

## PDF 解析后端：表格与图片

三级后端（`PDF_BACKEND=auto` 时自动降级）：

| 后端 | 依赖 | 能力 |
|---|---|---|
| A `pdf2md_skill` | `PDF2MD_SKILL_DIR`，OCR 时另需 Ark Key | 公式 LaTeX、双栏排序、扫描页 OCR、图片导出 |
| B `pymupdf4llm` | `pip install pymupdf4llm` | PyMuPDF 官方方案：`write_images` 导出图片、`table_strategy` 表格检测、标题层级、页眉页脚过滤 |
| C `pymupdf_enhanced` | 仅 `pymupdf`，无模型下载 | 多策略表格检测、三线表重建、图片导出、bbox 阅读顺序、孤立页码清理 |

**三线表（booktabs）问题**：学术论文表格常只有三条横线、没有竖线。PyMuPDF 的
`find_tables()` 默认 `lines` / `lines_strict` 依赖线条交点建网格，对这类表返回 0 个结果，
于是整张表被压成一行纯文本。直接对整页改用 `strategy="text"` 又会把标题、图注一起
吞成一张巨大伪表格。本 Skill 采用 **caption 锚定 + 词坐标对齐重建**：定位 `Table N` /
`表 N` 标题，以词数最多的行为锚点推断列边界，只保留与列结构对齐的行，再用质量校验
拒绝伪表格。图片块在元素收集时单独处理，不受表格 bbox 过滤。

开源借鉴出处：
- `pymupdf/RAG`（PyMuPDF4LLM）：`write_images` / `image_path` / `table_strategy` / `page_chunks`
- `Future-House/paper-qa` PR #1329：三线表漏检原因、caption anchoring、text-alignment runs
- HuggingFace `Agentic_RAG_PDF_QA` / `utils/layout_extractor.py`：用表格 bbox 屏蔽文本块、原位插入 Markdown 表格
- `taotaoboom/pdf2md-agent-skill`：跳过覆盖整页的扫描底图、页眉页脚过滤

更高质量的可选方案（需额外安装，适合扫描件 / 复杂跨页表）：`opendatalab/MinerU`
（`pip install magic-pdf`）、`docling-project/docling`（`pip install docling`）、
`datalab-to/marker`。它们输出的 Markdown 可直接用 `save_paper()` 入库，无需改动本 Skill。

## 集成到 Agent 框架

- **直接 import**：把本文件夹放进宿主项目，按上面的方式调用函数即可。
- **工具自动注册**：`get_tools()` 返回函数对象列表；`TOOL_SPECS` 提供 name/description/parameters 描述，可据此生成框架的 JSON Schema tool definition。
- **Skill 目录挂载**：把整个文件夹复制到宿主 Agent 的 skills 目录。Agent 读取本 `SKILL.md` 后即可在合适时机触发能力，无需修改框架源码。

## 边界与注意事项

- `app.py`（Streamlit）仅供本地调试与汇报演示，**不部署公网、不作为交付接口**；运行 `streamlit run app.py`。
- 知识抽取严格基于原文，`evidence` 保留原句；不得用常识补充或编造数值。
- 图谱构建与问答依赖运行中的 Neo4j；仅 PDF 解析与论文库管理可在无数据库时使用。
- `neo4j-graphrag` 的 `SimpleKGPipeline` 处于实验阶段（当前 API 为 `from_file` + `schema`，旧的 `from_pdf` / `entities` 已弃用），已封装为可选后端；默认路径不依赖它，更轻量且能精确控制文献回溯。
- 不要在 `SKILL.md`、代码或提交到 GitHub 的文件中写入真实密钥；统一使用 `.env`（已在 `.gitignore` 建议中排除）。
