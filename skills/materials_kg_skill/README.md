# materials_kg_skill

**材料文献智能解析与知识构建 Skill** —— 第 2 组 Agent 的可调用能力包。

> 交付物是**一个文件夹**（代码 + `SKILL.md`），不是一个部署好的网站。
> `app.py` 只是本地调试 / 汇报演示用的"驾驶舱"，Agent 真正调用的是下面这些 Python 函数。

```
PDF 文献 → Markdown（可人工校对） → LLM 抽取三元组 → Neo4j 知识图谱 → 可溯源 RAG 问答
```

每条知识都通过 `REPORTS` 关系回溯到文献节点，并保存原文证据句，因此从任一性能数值都能查到"哪篇文献、哪句话"。

---
> ⚡ **【队友使用必读】**
> 1. 确保已运行 `pip install -r requirements.txt` 安装依赖。
> 2. **重点**：将本目录下的 `.env.example` 复制一份并重命名为 `.env`。
> 3. 打开 `.env` 文件，填入**你自己的** API Key 和 **你自己电脑上** 的 Neo4j 数据库密码。
> 4. 请勿上传 `.env` 和 `.venv` 文件夹。
> 5. 确保你的 Neo4j 数据库已启动。配置完成后，直接调用 `get_tools()` 即可挂载本 Skill。

## 一、目录结构

| 文件 | 用途 | 是否交给 Agent |
|---|---|---|
| `SKILL.md` | Agent 读取的能力说明书（能力 / 触发条件 / 函数接口 / 本体 / 配置） | ✅ 核心 |
| `pdf_parser.py` | PDF → Markdown（三级后端 + 三线表重建 + 图片导出 + 页码清理）+ 论文库管理 | ✅ |
| `kg_builder.py` | 本体定义、分块、LLM 抽取、幂等写入 Neo4j、GraphRAG 可选后端 | ✅ |
| `rag_query.py` | 图谱召回、子图展开、上下文拼装、带来源的 RAG 问答 | ✅ |
| `config.py` | 环境变量与共享连接（Neo4j driver / LLM 客户端） | ✅ |
| `__init__.py` | `get_tools()` / `TOOL_SPECS`，供框架自动注册工具 | ✅ |
| `requirements.txt` | 依赖清单 | ✅ |
| `app.py` | Streamlit 本地工作台（上传解析 / 论文库 / 图谱问答） | ❌ 仅本地演示 |
| `.env.example` | 配置模板（**复制为 `.env` 后填写，不要提交真实密钥**） | 参考 |
| `examples/demo.py` | 离线全链路自测：不连 Neo4j / 不调 LLM / 不依赖真实 PDF，48 项断言 | 参考 |
| `examples/check_parsing.py` | 现场合成三线表 PDF，验证表格还原、图片导出、页码清理 | 参考 |
| `paper_library/<id>_assets/` | 运行时生成的图片目录，与 `.md` 同级，可一起上传 GitHub | 运行时 |
| `README.md` | 本文件，给人看的说明 | 参考 |

---

## 二、环境准备

| 工具 | 用途 | 备注 |
|---|---|---|
| Python 3.10+ | 运行代码 | 已验证 3.13 |
| PyCharm | 写代码 / 运行 | 你已安装；见下方操作 |
| Neo4j Desktop | 本地图数据库 | 新建数据库，记下 bolt URL、用户名、密码 |
| Git（可选） | 命令行上传 GitHub | 网页上传可不用 |

### PyCharm 里的操作

1. `File → Open`，选择本 `materials-kg-skill` 文件夹。
2. `Settings → Project → Python Interpreter → Add Interpreter`，选 Python 3.10+（建议新建虚拟环境）。
3. 在 PyCharm 底部打开 **Terminal**，执行：
   ```bash
   pip install -r requirements.txt
   ```
4. 安装并启动 Neo4j Desktop，创建一个本地数据库（记住密码）。
5. 复制配置模板并填写：
   ```bash
   cp .env.example .env
   ```
   用 DeepSeek 接口示例：
   ```
   LLM_API_KEY=sk-你的密钥
   LLM_BASE_URL=https://api.deepseek.com
   LLM_MODEL=deepseek-chat
   NEO4J_PASSWORD=你的数据库密码
   ```
   用 OpenAI 接口则保持 `.env.example` 的默认值即可。

> 核心抽取与问答**不依赖 embedding**，所以 DeepSeek 用户无需配置 `EMBEDDING_MODEL`。
> 只有可选后端 `build_from_pdf_graphrag()` 才需要 `pip install "neo4j-graphrag[openai]>=1.0,<2"`。

可选：启用方案 A（更好的公式 / 双栏 / 扫描件支持）
```bash
git clone https://github.com/taotaoboom/pdf2md-agent-skill.git
```
然后在 `.env` 里设置 `PDF2MD_SKILL_DIR` 为该仓库绝对路径；中文扫描件 OCR 还需按其文档配置 `ARK_API_KEY`。

**推荐再装一个官方开源方案**（纯 Python，无需下载模型）：
```bash
pip install pymupdf4llm
```
不装也没关系：程序会自动降级到 `pymupdf_enhanced`（纯 PyMuPDF 增强后端），**同样能识别三线表和导出图片**。侧边栏会显示当前实际使用的后端。

---

## 三、快速使用

### 1. Python 调用（Agent 集成方式）

```python
from pdf_parser import parse_pdf_to_markdown
from kg_builder import build_knowledge_graph
from rag_query import rag_query

# 一步到位也可以：from kg_builder import build_from_pdf
md = parse_pdf_to_markdown("papers/6082_t6.pdf", language="auto")
# ...此处可插入人工校对：save_paper("6082_t6", edited_md)
stats = build_knowledge_graph(md, paper_id="6082_t6",
                              paper_title="6082 铝合金 T6 时效组织与性能")
print(stats)

answer = rag_query("6082 铝合金 T6 时效后的抗拉强度是多少？")
print(answer["answer"])
for src in answer["sources"]:
    print(src["index"], src["title"], src["paper_id"])
```

若宿主框架需要自动发现工具：

```python
from materials_kg_skill import get_tools, TOOL_SPECS
for fn in get_tools():
    framework.register(fn)          # 或按 TOOL_SPECS 生成 JSON Schema
```

> ⚠️ **挂载方式说明**：目录名 `materials-kg-skill` 含连字符，不能直接作为 Python 包名。
> 各模块已做双模式导入兼容，任选其一即可：
> 1. **推荐**：把本目录加入 `sys.path`，直接 `import pdf_parser / kg_builder / rag_query`（上面的快速示例即为此方式）；
> 2. 把文件夹重命名为 `materials_kg_skill`（下划线）后再用 `from materials_kg_skill import ...`。
> 上传到 GitHub 的仓库名可保留连字符，不影响使用。

### 2. 本地工作台（汇报演示）

```bash
streamlit run app.py
```

浏览器会自动打开，三个 Tab：

- **上传解析**：上传 PDF → 解析 → 在文本框里人工校对 → 点"确认入库并构建知识图谱"。
- **论文库**：查看 / 下载已入库的 Markdown。
- **知识图谱 & 问答**：节点统计、pyvis 子图、RAG 问答（显示答案、文献来源、检索上下文）。

侧边栏会显示当前配置状态；若 Neo4j 或 LLM 未配置会给出提示。

### 3. 离线验证（无需数据库 / 无需 API Key）

```bash
python examples/demo.py              # 48 项断言，mock 全链路
python examples/check_parsing.py     # 现场合成 PDF，验证表格 / 图片 / 页码
```

`demo.py` 用假的 LLM 抽取器和假的 Neo4j driver 跑通"入库 → 抽取 → 写入 → 检索 → 答案"；
`check_parsing.py` 会现场生成一个与你截图相同的三线表 PDF，直接检验解析效果。

### 4. 重新解析已有文献（重要）

如果之前已经解析过（比如 `paper_library/testpdf_01.md` 里表格是压扁的纯文本、图片丢失），
**旧文件不会自动修复，需要重新解析一次**。任选一种：

- **界面**：`streamlit run app.py` → 「上传解析」Tab 重新上传同一个 PDF →
  勾选「导出 PDF 图片」→ 点 ① 解析 → 校对 → ② 入库。表格仍不对就把「表格检测策略」改成 `text`。
- **代码**：
  ```python
  from pdf_parser import parse_pdf_to_markdown

  md = parse_pdf_to_markdown(
      "你的论文.pdf",
      paper_id="testpdf_01",
      backend="pymupdf_enhanced",   # 或 auto
      write_images=True,
  )
  ```
  生成的 `.md` 会覆盖旧文件，图片写入 `paper_library/testpdf_01_assets/`。

> ⚠️ 之前由该文献构建的知识图谱节点不会自动刷新，建议在界面里再点一次
> 「确认入库并构建知识图谱」（写入是幂等的，不会产生重复节点）。

---

## 四、解析后端：表格与图片（本次修复）

### 问题

学术论文里 `Table 2 Alloy composition...` 这类**三线表（booktabs）**只有三条横线、没有竖线。
旧版 `pdf_parser.py` 用 `get_text("blocks")` 提纯文本，并依赖 `find_tables()` 默认策略：

- `lines` / `lines_strict` 靠线条交点建网格 → 无竖线时**返回 0 个表格**，整张表被压成一行纯文本
  （`AA6082 0.6-1.2 ... Bal. AA6086 1.01 ... Bal.`）。
- 图片块被 `b[6] == 0` 直接过滤 → **图片全部丢失**，只剩 `Fig. 1.` 图注文字。
- 页脚孤立页码（如 `1874`、`1869`）混进正文。

### 修复

三级后端，`PDF_BACKEND=auto` 自动降级：

| 后端 | 依赖 | 能力 |
|---|---|---|
| A `pdf2md_skill` | `PDF2MD_SKILL_DIR` | 公式 LaTeX、双栏排序、扫描页 OCR、图片导出 |
| B `pymupdf4llm` | `pip install pymupdf4llm` | PyMuPDF 官方方案：`write_images` + `table_strategy` + 页眉页脚过滤 |
| C `pymupdf_enhanced` | 仅 `pymupdf`，**无需下载模型** | 多策略表格、三线表重建、图片导出、bbox 阅读顺序、页码清理 |

方案 C 的关键点：

1. **图片**：`get_text("dict")` 的 `type=1` 图片块单独导出到 `<paper_id>_assets/`，
   生成 `![Figure pN](<assets>/xxx.png)` 相对引用；永不因落在表格 bbox 内被丢弃。
2. **三线表**：借鉴 `Future-House/paper-qa` PR #1329 的 caption anchoring + text-alignment runs
   —— 定位 `Table N` / `表 N` 标题，把词按 y 聚成行，以词数最多的行为锚点推断列中心，
   只保留与列结构对齐的行，再生成 GFM 表格。
3. **不盲目用 `strategy="text"`**：实测对整页使用会把标题、图注一起吞成巨大伪表格，
   因此只在 caption 下方受控使用，并用面积 / 列数 / 长句校验拒绝伪表格。
4. **阅读顺序**：按 bbox 的 y（分行）与 x 排序文本块 / 图片块 / 表格，表格在正确位置插入，
   落在表格区域内的文本块跳过（借鉴 `Agentic_RAG_PDF_QA` 的 `layout_extractor.py`）。
5. **页码清理**：靠近页面上下边缘的短数字、独立成行的纯数字页码被剔除；
   `| 310 | ... |` 这类表格行不会被误伤。

### 开源借鉴出处

- `pymupdf/RAG`（PyMuPDF4LLM）：`write_images` / `image_path` / `table_strategy` / `page_chunks`
- `Future-House/paper-qa` PR #1329：三线表漏检原因与 caption 锚定 / 文本对齐检测策略
- HuggingFace `Agentic_RAG_PDF_QA` / `utils/layout_extractor.py`：表格 bbox 屏蔽 + 原位插入
- `taotaoboom/pdf2md-agent-skill`：跳过覆盖整页的扫描底图、页眉页脚过滤

### 还想更高质量？

扫描件、跨页表、复杂公式可换重型开源方案（输出 Markdown 后用 `save_paper()` 入库，
无需改本 Skill 代码）：`opendatalab/MinerU`（`pip install magic-pdf`）、
`docling-project/docling`（`pip install docling`）、`datalab-to/marker`。注意后两者的许可证限制。

### 验证结果

`python examples/check_parsing.py` 现场合成与截图同款的 PDF，输出：

```
Table 2 Alloy composition of AA6082 and AA6086 [82,87].

Alloy Major element (wt.%)

| Alloy | Mg | Si | Fe | Mn | Cu | Zn | Ti | Al |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| AA6082 | 0.6-1.2 | 0.7-1.3 | <0.5 | 0.4-1 | <1 | <0.2 | <0.1 | Bal. |
| AA6086 | 1.01 | 1.58 | 0.18 | 0.71 | 0.55 | 0.17 | 0.04 | Bal. |

![Figure p1](check_lines_strict_assets/check_lines_strict_p1_2.png)

Fig. 1. Microstructure of the AA6082 alloy.
```

表格还原 ✅ · 图片导出 ✅ · 孤立页码 `1874` 消失 ✅。

---

## 五、知识图谱本体

**节点**：`Material` / `Property` / `Value` / `Process` / `Literature` / `Knowledge`

**关系**：

```
(Literature)-[:REPORTS]->(Knowledge)
(Knowledge)-[:ABOUT_MATERIAL]->(Material)
(Knowledge)-[:HAS_PROPERTY]->(Property)
(Property)-[:HAS_VALUE]->(Value)
(Material)-[:PROCESSED_BY]->(Process)
```

`Knowledge` 节点带 `statement`（事实陈述）、`evidence`（原文证据句）、`paper_id`、`chunk_id`、`uid`。写入全部使用 `MERGE`，重复入库同一篇文献不会产生重复节点。

在 Neo4j Browser 里查看示例：
```cypher
MATCH (l:Literature)-[:REPORTS]->(k:Knowledge)-[:HAS_PROPERTY]->(p:Property)-[:HAS_VALUE]->(v:Value)
RETURN l, k, p, v LIMIT 50;
```

---

## 六、上传到 GitHub（网页版，零命令行）

1. 登录 github.com → 右上角 `+` → **New repository**，名字填 `materials-kg-skill`，选 **Public** → **Create repository**。
2. 进入仓库 → **Add file** → **Upload files**，把本文件夹里的文件全部拖进去。
3. 下方填提交说明（如 `initial commit`）→ **Commit changes**。
4. 刷新页面即可。以后改代码重复第 2–3 步。

> ⚠️ **不要上传 `.env`（含真实密钥）和 `.venv/`（虚拟环境，体积可达数百 MB）**。
> 仓库里已附 `.gitignore`，内容包含：
> ```
> .env
> .venv/
> paper_library/
> __pycache__/
> *.py[cod]
> ```
> 若要把已解析的 Markdown 和图片一起上传，单独把 `paper_library/<id>.md`
> 与 `paper_library/<id>_assets/` 拖进去即可（图片引用是相对路径，GitHub 能正常显示）。

可选命令行方式：
```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/你的用户名/materials-kg-skill.git
git push -u origin main
```

---

## 七、常见问题

**Q：没有 Neo4j / 没有 API Key，能先验证代码吗？**
能。运行 `python examples/demo.py`，它用 mock 对象跑通全链路，不需要任何外部服务。

**Q：DeepSeek 能用吗？**
能。设置 `LLM_BASE_URL=https://api.deepseek.com`、`LLM_MODEL=deepseek-chat`。核心链路只用 chat completions，兼容任意 OpenAI 格式接口。程序会自动尝试 `response_format={"type":"json_object"}`，接口不支持时自动降级为普通模式。

**Q：指南里的 `from_pdf=True` 为什么没用？**
`neo4j-graphrag` 该 API 已弃用，当前版本用 `from_file=True` 和 `schema={"node_types","relationship_types","patterns"}`。本项目默认走自研轻量路径（依赖少、能精确控制文献回溯），GraphRAG 封装在 `build_from_pdf_graphrag()` 中按新 API 实现。

**Q：表格还是压扁的纯文本 / 图片还是没出来？**
先把 `PDF_WRITE_IMAGES=true` 确认，重新解析一次（旧 `.md` 不会自动修复，见「三、4」）。
在界面把「表格检测策略」改成 `text`，或直接指定 `backend="pymupdf_enhanced"`。
若表格没有 `Table N` 标题，caption 锚定无法定位，可在解析后人工在 Markdown 里补表格。
运行 `python examples/check_parsing.py` 可快速确认当前环境的解析能力。

**Q：装了 `pymupdf4llm` 和没装有什么区别？**
装了走官方方案 B（标题层级、双栏排序、页眉页脚过滤更好）；没装自动降级到方案 C，
三线表与图片同样能识别。侧边栏会显示当前实际后端。

**Q：中文论文解析效果不好？**
配置 `PDF2MD_SKILL_DIR` 启用方案 A 的 `md_convert_zh.py`（含 OCR）；或在 Streamlit 里人工校对后再入库。

**Q：检索不到知识？**
确认已调用 `ensure_indexes()`（`build_knowledge_graph` 默认会做）；程序在全文索引无结果时会自动降级到 `CONTAINS` 匹配。若图谱本身为空，需要先入库文献。

---

## 八、交付清单对照

| 指南要求 | 本仓库 |
|---|---|
| `SKILL.md` | ✅ 含 frontmatter + 能力 + 触发条件 + 函数签名 + 本体 + 配置 + 集成方式 |
| `pdf_parser.py` | ✅ 三级解析后端 + 三线表重建 + 图片导出 + 页码清理 + 论文库 |
| `kg_builder.py` | ✅ 本体 + 抽取 + 幂等写入 + 文献回溯 + GraphRAG 可选后端 |
| `rag_query.py` | ✅ 全文索引 / CONTAINS 双路召回 + 带来源答案 |
| `app.py`（Streamlit） | ✅ 三 Tab，本地运行，不部署 |
| `requirements.txt` | ✅ 核心 / 界面 / 可选三组 |
| `README.md` | ✅ 本文件 |
| 额外 | `config.py`、`__init__.py`（get_tools / TOOL_SPECS）、`.env.example`、`.gitignore`、`examples/demo.py`、`examples/check_parsing.py` |
