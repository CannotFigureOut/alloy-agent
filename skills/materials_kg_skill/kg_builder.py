"""Markdown → Neo4j 材料知识图谱（核心后端）。

默认路径（simple）：分块 → LLM 抽取结构化知识 → 幂等写入 Neo4j，
每条 Knowledge 都通过 REPORTS 关系回溯到 Literature 文献节点。
可选路径（graphrag）：Neo4j 官方 SimpleKGPipeline，按当前 API（from_file + schema）。

本体（Ontology）
  节点：Material / Property / Value / Process / Literature / Knowledge
  关系：(Literature)-[:REPORTS]->(Knowledge)
        (Knowledge)-[:ABOUT_MATERIAL]->(Material)
        (Knowledge)-[:HAS_PROPERTY]->(Property)
        (Property)-[:HAS_VALUE]->(Value)
        (Material)-[:PROCESSED_BY]->(Process)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
try:  # 包内导入 / 顶层模块导入 双兼容
    from .config import get_driver, get_llm_client, get_settings
    from .pdf_parser import sanitize_paper_id
except ImportError:  # pragma: no cover
    from config import get_driver, get_llm_client, get_settings
    from pdf_parser import sanitize_paper_id

# ------------------------------ 本体定义 ------------------------------

NODE_TYPES: list[dict[str, str]] = [
    {"label": "Material", "description": "材料牌号或体系，如 6082、6061、Al-Si 合金、7xxx 系铝合金"},
    {"label": "Property", "description": "材料性能指标名称，如抗拉强度、屈服强度、延伸率、疲劳寿命、耐腐蚀性"},
    {"label": "Value", "description": "性能的具体数值及单位，如 310 MPa、8.5 %、180 HV"},
    {"label": "Process", "description": "加工或热处理工艺，如 T6 时效、固溶处理、挤压、焊接、轧制"},
    {"label": "Knowledge", "description": "从文献中抽取的一条可被证据支撑的知识陈述"},
    {"label": "Literature", "description": "文献来源，带 paper_id、标题和文件名"},
]

RELATIONSHIP_TYPES: list[dict[str, str]] = [
    {"label": "REPORTS", "description": "文献报道了某条知识"},
    {"label": "ABOUT_MATERIAL", "description": "知识涉及某材料"},
    {"label": "HAS_PROPERTY", "description": "知识涉及某性能指标"},
    {"label": "HAS_VALUE", "description": "性能指标对应的数值"},
    {"label": "PROCESSED_BY", "description": "材料经过某工艺处理"},
]

PATTERNS: list[tuple[str, str, str]] = [
    ("Literature", "REPORTS", "Knowledge"),
    ("Knowledge", "ABOUT_MATERIAL", "Material"),
    ("Knowledge", "HAS_PROPERTY", "Property"),
    ("Property", "HAS_VALUE", "Value"),
    ("Material", "PROCESSED_BY", "Process"),
]

# 传给 SimpleKGPipeline 的 schema（当前 API 使用 node_types / relationship_types / patterns）
GRAPHRAG_SCHEMA: dict[str, Any] = {
    "node_types": NODE_TYPES,
    "relationship_types": RELATIONSHIP_TYPES,
    "patterns": PATTERNS,
    "additional_node_types": False,
}

# ------------------------------ LLM 抽取 ------------------------------

EXTRACTION_SYSTEM_PROMPT = """你是一位材料科学文献信息抽取专家。
请从给定文本中提取材料知识。严格只输出 JSON，不要任何解释或 Markdown 标记。

JSON 结构要求：
{
  "entities": [{"name": "实体名", "type": "Material/Property/Value/Process", "description": "简述"}],
  "knowledge": [
    {
      "statement": "一句完整的知识陈述",
      "evidence": "原文支撑句",
      "materials": ["实体名"],
      "properties": ["实体名"],
      "values": ["实体名"],
      "processes": ["实体名"]
    }
  ]
}
规则：
1. 宁缺毋滥，只提取明确的事实。
2. materials/properties/values/processes 里的名字必须在 entities 中出现过。
3. 如果没有提取到，返回 {"entities": [], "knowledge": []}。"""


def build_extraction_prompt(chunk: str, paper_id: str, title: str) -> str:
    """构造单次抽取的 user prompt。"""
    return f"""文献标识: {paper_id}
文献标题: {title or '(未提供)'}

文本片段:
\"\"\"
{chunk}
\"\"\"

请从上述片段抽取实体与知识，输出如下 JSON 结构：
{{
  "entities": [
    {{"name": "6082", "type": "Material", "description": "铝合金牌号"}},
    {{"name": "抗拉强度", "type": "Property", "description": "力学性能指标"}},
    {{"name": "310 MPa", "type": "Value", "description": "带单位的数值"}},
    {{"name": "T6时效", "type": "Process", "description": "热处理工艺"}}
  ],
  "knowledge": [
    {{
      "statement": "6082铝合金经T6时效后抗拉强度达到310 MPa",
      "evidence": "原文中支撑该结论的句子",
      "materials": ["6082"],
      "properties": ["抗拉强度"],
      "values": ["310 MPa"],
      "processes": ["T6时效"]
    }}
  ]
}}

字段规则：
- type 只能是 Material / Property / Value / Process 之一。
- statement 是一句完整、独立可读的事实陈述。
- materials / properties / values / processes 是字符串数组，元素须与 entities 中的名称一致；没有则给空数组 []。
- 宁缺毋滥：不确定的信息不要写入。"""


def _parse_json_loose(content: str) -> dict[str, Any]:
    """容错解析 LLM 返回的 JSON（去掉代码围栏、截取首尾花括号）。"""
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    raise ValueError(f"LLM 返回的 JSON 无法解析，前 200 字符：{text[:200]}")


def _call_llm_json(messages: list[dict[str, str]]) -> dict[str, Any]:
    """调用 OpenAI 兼容接口；超时或报错只重试一次，避免卡死。"""
    s = get_settings()
    client = get_llm_client()

    try:
        print(f"  -> 正在调用大模型 (JSON模式)...")
        kwargs: dict[str, Any] = {
            "model": s.llm_model,
            "messages": messages,
            "temperature": s.temperature,
            "timeout": 60,  # 60秒不回复就拉倒
        }
        # 优先尝试 JSON 模式
        kwargs["response_format"] = {"type": "json_object"}
        response = client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        return _parse_json_loose(content)
    except Exception as exc:
        print(f"  ⚠️ JSON模式失败或超时: {exc}，尝试普通模式...")
        try:
            # 降级：去掉 JSON 模式再试最后一次
            kwargs.pop("response_format", None)
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            return _parse_json_loose(content)
        except Exception as final_exc:
            # 如果还是失败，直接抛出异常，让外层捕获并跳过这个块
            raise RuntimeError(f"大模型彻底失败：{final_exc}")


def extract_from_chunk(chunk: str, paper_id: str, title: str, chunk_index: int = 0) -> dict[str, Any]:
    """对单个文本块调用 LLM 抽取实体与知识。可作为自定义 extractor 的默认实现。"""
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": build_extraction_prompt(chunk, paper_id, title)},
    ]
    data = _call_llm_json(messages)
    if not isinstance(data, dict):
        raise ValueError("LLM 抽取结果不是 JSON 对象")
    return data


# ------------------------------ 分块与行构造 ------------------------------

def split_markdown(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    """按段落边界分块，超长段落再硬切；尽量保持标题与段落完整。"""
    s = get_settings()
    size = chunk_size or s.chunk_size
    step_overlap = overlap if overlap is not None else s.chunk_overlap
    step = max(1, size - max(0, step_overlap))

    paragraphs = re.split(r"\n\s*\n", text.strip())
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(buffer) + len(para) + 2 <= size:
            buffer = f"{buffer}\n\n{para}".strip()
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        if len(para) > size:
            for start in range(0, len(para), step):
                piece = para[start : start + size]
                if piece.strip():
                    chunks.append(piece)
        else:
            buffer = para
    if buffer:
        chunks.append(buffer)
    return chunks


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _pair_property_values(props: list[str], values: list[str]) -> list[list[str]]:
    """把属性名与数值配对，避免 Cypher 里的笛卡尔积（每个属性只挂对应数值）。"""
    if not props or not values:
        return []
    if len(props) == len(values):
        return [[p, v] for p, v in zip(props, values)]
    if len(values) == 1:
        return [[p, values[0]] for p in props]
    if len(props) == 1:
        return [[props[0], v] for v in values]
    return [[props[0], values[0]]]


def _stable_uid(paper_id: str, chunk_index: int, item_index: int, statement: str) -> str:
    raw = f"{paper_id}|{chunk_index}|{item_index}|{statement}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def build_rows_from_extraction(
    data: dict[str, Any], paper_id: str, chunk_index: int
) -> list[dict[str, Any]]:
    """把 LLM 抽取结果转成待写入 Neo4j 的知识行。"""
    rows: list[dict[str, Any]] = []
    entities = data.get("entities") or []

    for idx, item in enumerate(data.get("knowledge") or []):
        if not isinstance(item, dict):
            continue
        statement = str(item.get("statement") or "").strip()
        if not statement:
            continue
        props = _clean_list(item.get("properties"))
        values = _clean_list(item.get("values"))
        rows.append(
            {
                "uid": _stable_uid(paper_id, chunk_index, idx, statement),
                "statement": statement[:4000],
                "evidence": str(item.get("evidence") or "")[:2000],
                "chunk_id": chunk_index,
                "materials": _clean_list(item.get("materials")),
                "properties": props,
                "values": values,
                "pairs": _pair_property_values(props, values),
                "processes": _clean_list(item.get("processes")),
            }
        )

    # 兼容"只给了 entities + relations"的 LLM 输出：把每条关系转成一条知识。
    if not rows and isinstance(data.get("relations"), list):
        id_to_type = {
            str(e.get("name", "")).strip(): str(e.get("type", "")).strip()
            for e in entities
            if isinstance(e, dict)
        }
        for idx, rel in enumerate(data["relations"]):
            if not isinstance(rel, dict):
                continue
            src = str(rel.get("source") or "").strip()
            tgt = str(rel.get("target") or "").strip()
            label = str(rel.get("relation") or "").strip()
            if not (src and tgt and label):
                continue
            statement = f"{src} {label} {tgt}"
            row = {
                "uid": _stable_uid(paper_id, chunk_index, idx, statement),
                "statement": statement,
                "evidence": "",
                "chunk_id": chunk_index,
                "materials": [],
                "properties": [],
                "values": [],
                "pairs": [],
                "processes": [],
            }
            for name in (src, tgt):
                kind = id_to_type.get(name, "")
                key = {
                    "Material": "materials",
                    "Property": "properties",
                    "Value": "values",
                    "Process": "processes",
                }.get(kind)
                if key:
                    row[key].append(name)
            rows.append(row)
    return rows


# ------------------------------ Neo4j 写入 ------------------------------
CYPHER_LITERATURE = """
MERGE (l:Literature {paper_id: $paper_id})
SET l.title = $title, l.source_file = $source_file, l.updated_at = datetime()
"""

CYPHER_KNOWLEDGE = """
UNWIND $rows AS r
MERGE (k:Knowledge {uid: r.uid})
SET k.statement = r.statement,
    k.evidence = r.evidence,
    k.paper_id = $paper_id,
    k.chunk_id = r.chunk_id
WITH k
MATCH (l:Literature {paper_id: $paper_id})
MERGE (l)-[:REPORTS]->(k)
"""

CYPHER_MATERIAL = """
UNWIND $rows AS r
MATCH (k:Knowledge {uid: r.uid})
UNWIND r.materials AS name
MERGE (m:Material {name: name})
MERGE (k)-[:ABOUT_MATERIAL]->(m)
"""

CYPHER_PROPERTY = """
UNWIND $rows AS r
MATCH (k:Knowledge {uid: r.uid})
UNWIND r.properties AS pname
MERGE (p:Property {name: pname})
MERGE (k)-[:HAS_PROPERTY]->(p)
"""

CYPHER_VALUE = """
UNWIND $rows AS r
UNWIND r.pairs AS pair
MERGE (p:Property {name: pair[0]})
MERGE (v:Value {name: pair[1]})
MERGE (p)-[:HAS_VALUE]->(v)
"""

CYPHER_PROCESS = """
UNWIND $rows AS r
UNWIND r.materials AS mat
MERGE (m:Material {name: mat})
WITH r, m
UNWIND r.processes AS pr
MERGE (proc:Process {name: pr})
MERGE (m)-[:PROCESSED_BY]->(proc)
"""


def _persist(
    tx: Any,
    paper_id: str,
    title: str,
    source_file: str,
    rows: list[dict[str, Any]],
) -> None:
    params = {"paper_id": paper_id, "title": title, "source_file": source_file}
    tx.run(CYPHER_LITERATURE, **params)
    if not rows:
        return
    tx.run(CYPHER_KNOWLEDGE, paper_id=paper_id, rows=rows)
    tx.run(CYPHER_MATERIAL, rows=rows)
    tx.run(CYPHER_PROPERTY, rows=rows)
    tx.run(CYPHER_VALUE, rows=rows)
    tx.run(CYPHER_PROCESS, rows=rows)


def ensure_indexes() -> list[str]:
    """创建全文索引与主键约束（兼容 Neo4j 3.5 版本）"""
    statements = [
        # Neo4j 3.5 的全文索引调用方式
        "CALL db.index.fulltext.createNodeIndex('knowledge_text', ['Knowledge'], ['statement', 'evidence'])",
        "CALL db.index.fulltext.createNodeIndex('literature_text', ['Literature'], ['title', 'paper_id'])",
        "CALL db.index.fulltext.createNodeIndex('entity_text', ['Material', 'Property'], ['name'])",
        # Neo4j 3.5 的约束创建方式
        "CREATE CONSTRAINT ON (k:Knowledge) ASSERT k.uid IS UNIQUE",
        "CREATE CONSTRAINT ON (l:Literature) ASSERT l.paper_id IS UNIQUE",
    ]
    s = get_settings()
    with get_driver().session(database=s.neo4j_database) as session:
        for cypher in statements:
            try:
                session.run(cypher)
            except Exception as e:
                # 因为去掉了 IF NOT EXISTS，如果索引已经存在会报错，这里直接捕获并忽略，保证代码可以反复运行
                pass
    return statements


# ------------------------------ 公共入口 ------------------------------

Extractor = Callable[[str, str, str, int], dict[str, Any]]


def build_knowledge_graph(
        markdown_text: str,
        paper_id: str,
        paper_title: str | None = None,
        source_file: str | None = None,
        extractor: Extractor | None = None,
        ensure_indices: bool = True,
) -> dict[str, Any]:
    """从 Markdown 抽取知识并写入 Neo4j。
    使用并发线程池大幅提升大模型抽取速度。
    """
    s = get_settings()
    pid = sanitize_paper_id(paper_id)
    title = paper_title or pid
    markdown_text = re.sub(r'!\[.*?\]\(.*?\)', '\n[此处有图片，已省略]\n', markdown_text)
    chunks = split_markdown(markdown_text)
    extract_fn = extractor or extract_from_chunk

    all_rows: list[dict[str, Any]] = []
    entity_names: set[str] = set()
    errors: list[dict[str, str]] = []

    print(f"\n========== 开始并发处理文献: {pid} ==========")
    print(f"共切分为 {len(chunks)} 个文本块，启动 3 个并发线程...")

    def process_chunk(index_and_chunk):
        index, chunk = index_and_chunk
        try:
            data = extract_fn(chunk, pid, title, index)
            rows = build_rows_from_extraction(data, pid, index)
            return index, data, rows, None
        except Exception as exc:
            return index, {}, [], str(exc)


    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(process_chunk, (i, c)): i for i, c in enumerate(chunks)}

        for future in as_completed(futures):
            index, data, rows, error = future.result()
            if error:
                print(f"  ❌ 第 {index + 1} 块解析失败: {error}")
                errors.append({"chunk_index": str(index), "error": error})
            else:
                for ent in data.get("entities") or []:
                    if isinstance(ent, dict) and ent.get("name"):
                        entity_names.add(str(ent["name"]).strip())
                all_rows.extend(rows)
                print(f"  ✅ 第 {index + 1} 块完成，提取了 {len(rows)} 条知识。")

    print(f"========== 抽取完成，共获得 {len(all_rows)} 条知识，准备写入数据库 ==========\n")

    if ensure_indices:
        ensure_indexes()

    driver = get_driver()
    with driver.session(database=s.neo4j_database) as session:
        session.execute_write(_persist, pid, title, source_file or "", all_rows)

    return {
        "paper_id": pid,
        "title": title,
        "chunks": len(chunks),
        "knowledge_nodes": len(all_rows),
        "entities": len(entity_names),
        "errors": errors,
    }


def build_from_pdf(
    pdf_path: str,
    paper_id: str | None = None,
    paper_title: str | None = None,
    language: str = "auto",
) -> dict[str, Any]:
    """组合入口：PDF → Markdown → 知识图谱（对应指南中的 parse_pdf_and_build_kg）。"""
    try:
        from .pdf_parser import parse_pdf_to_markdown
    except ImportError:  # pragma: no cover
        from pdf_parser import parse_pdf_to_markdown

    path = str(pdf_path)
    pid = paper_id or sanitize_paper_id(path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    markdown = parse_pdf_to_markdown(path, language=language, paper_id=pid)
    return build_knowledge_graph(markdown, pid, paper_title, source_file=path)


def build_from_pdf_graphrag(
    pdf_path: str,
    paper_id: str | None = None,
    paper_title: str | None = None,
) -> dict[str, Any]:
    """可选高阶后端：Neo4j 官方 SimpleKGPipeline（当前 API：from_file + schema）。

    注意该模块处于实验阶段，API 可能变化；核心功能不依赖本函数。
    """
    try:
        from neo4j_graphrag.llm import OpenAILLM
        from neo4j_graphrag.embeddings import OpenAIEmbeddings
        from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline
    except ImportError as exc:
        raise RuntimeError(
            "未安装 neo4j-graphrag。请运行 pip install \"neo4j-graphrag[openai]>=1.0,<2\""
        ) from exc

    s = get_settings()
    pid = sanitize_paper_id(paper_id or Path(pdf_path).stem)

    def _llm() -> Any:
        kwargs = {
            "model_name": s.llm_model,
            "model_params": {"temperature": s.temperature},
            "api_key": s.llm_api_key,
        }
        try:
            return OpenAILLM(base_url=s.llm_base_url, **kwargs) if s.llm_base_url else OpenAILLM(**kwargs)
        except TypeError:
            return OpenAILLM(**kwargs)

    def _embedder() -> Any:
        kwargs = {"model": s.embedding_model, "api_key": s.llm_api_key}
        try:
            return OpenAIEmbeddings(base_url=s.embedding_base_url or s.llm_base_url, **kwargs) if (
                s.embedding_base_url or s.llm_base_url
            ) else OpenAIEmbeddings(**kwargs)
        except TypeError:
            return OpenAIEmbeddings(**kwargs)

    pipeline = SimpleKGPipeline(
        llm=_llm(),
        driver=get_driver(),
        embedder=_embedder(),
        neo4j_database=s.neo4j_database,
        from_file=True,  # 当前 API；旧版 from_pdf 已弃用
        schema=GRAPHRAG_SCHEMA,
        on_error="RAISE",
    )
    result = asyncio.run(
        pipeline.run_async(
            file_path=str(pdf_path),
            document_metadata={"paper_id": pid, "title": paper_title or pid},
        )
    )

    # 补建 Literature 节点，并把本次 Document 关联的实体接上 REPORTS，便于统一 RAG 查询。
    link_cypher = """
    MATCH (d:Document) WHERE d.paper_id = $paper_id
    MERGE (l:Literature {paper_id: $paper_id})
      SET l.title = $title, l.updated_at = datetime()
    MERGE (l)-[:HAS_DOCUMENT]->(d)
    WITH l, d
    MATCH (d)-[*1..3]->(n)
    WHERE n:Material OR n:Property OR n:Process OR n:Value
    MERGE (l)-[:REPORTS]->(n)
    """
    with get_driver().session(database=s.neo4j_database) as session:
        session.run(link_cypher, paper_id=pid, title=paper_title or pid)

    return {
        "paper_id": pid,
        "backend": "graphrag",
        "pipeline_result": str(result)[:500],
    }
