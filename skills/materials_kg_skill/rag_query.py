"""基于知识图谱的 RAG 检索与问答。

流程：用户问题 → 提取关键词 → Neo4j 全文索引 / CONTAINS 召回 Knowledge 节点
      → Cypher 展开文献、材料、性能、数值、工艺 → 拼成结构化 context
      → LLM 生成带 [来源 N] 标注的答案。
"""
from __future__ import annotations

import re
from typing import Any

try:  # 包内导入 / 顶层模块导入 双兼容
    from .config import get_driver, get_llm_client, get_settings
except ImportError:  # pragma: no cover
    from config import get_driver, get_llm_client, get_settings

STOPWORDS = {
    "的", "了", "和", "与", "在", "是", "有", "对", "为", "中", "上", "下", "及", "或", "等",
    "多少", "什么", "哪些", "如何", "怎么", "请问", "分别", "以及", "吗", "呢", "啊", "请",
    "材料", "性能", "文献", "这篇", "该", "一个", "可以", "请问", "告诉", "关于",
    "the", "of", "and", "for", "in", "on", "what", "how", "which", "is", "are", "with",
}

# 用于从连续中文串中切出有意义词组的虚词
ZH_SPLIT_RE = re.compile(r"的|和|与|在|是|有|对|为|中|上|下|及|或|等|多少|什么|哪些|如何|怎么|请问|分别|以及|吗|呢")

RETURN_CLAUSE = """
RETURN k.statement AS statement,
       k.evidence AS evidence,
       k.paper_id AS paper_id,
       l.title AS title,
       collect(DISTINCT m.name) AS materials,
       collect(DISTINCT p.name) AS properties,
       collect(DISTINCT v.name) AS values,
       collect(DISTINCT proc.name) AS processes,
       score
"""

def extract_keywords(question: str, limit: int = 10) -> list[str]:
    """从自然语言问题中提取检索关键词（英文 token + 中文虚词切分）。"""
    english = re.findall(r"[A-Za-z][A-Za-z0-9\-]*|\d{3,}[A-Za-z]*", question)
    chinese: list[str] = []
    for segment in re.findall(r"[\u4e00-\u9fff]+", question):
        for part in ZH_SPLIT_RE.split(segment):
            part = part.strip()
            if not part or part in STOPWORDS:
                continue
            if len(part) <= 8:
                chinese.append(part)
            else:  # 超长片段按滑窗取若干 4 字词，避免漏掉长术语
                chinese.extend(part[i : i + 4] for i in range(0, min(len(part), 12), 3))

    keywords: list[str] = []
    for word in english + chinese:
        if len(word) < 2 or word.lower() in STOPWORDS or word in keywords:
            continue
        keywords.append(word)
    return keywords[:limit]


def _lucene_query(keywords: list[str]) -> str:
    escaped = [kw.replace('"', r'\"') for kw in keywords[:8]]
    return " OR ".join(f'"{kw}"' for kw in escaped) if escaped else ""


def _run_rows(cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    s = get_settings()
    with get_driver().session(database=s.neo4j_database) as session:
        return session.run(cypher, **params).data()


def retrieve_context(question: str, top_k: int | None = None) -> dict[str, Any]:
    """从知识图谱召回与问题相关的子图，返回 snippets 与格式化上下文。

    Returns:
        {"question", "keywords", "snippets", "context", "method"}
        method 取值：fulltext / contains / none。
    """
    s = get_settings()
    k = top_k or s.top_k
    keywords = extract_keywords(question)
    snippets: list[dict[str, Any]] = []
    method = "none"

    if keywords:
        # 路径 1：全文索引（需要先调用 kg_builder.ensure_indexes()）
        try:
            cypher = """
            CALL db.index.fulltext.queryNodes('knowledge_text', $fq) YIELD node AS k, score
            MATCH (k)<-[:REPORTS]-(l:Literature)
            OPTIONAL MATCH (k)-[:ABOUT_MATERIAL]->(m:Material)
            OPTIONAL MATCH (k)-[:HAS_PROPERTY]->(p:Property)
            OPTIONAL MATCH (p)-[:HAS_VALUE]->(v:Value)
            OPTIONAL MATCH (m)-[:PROCESSED_BY]->(proc:Process)
            WITH k, l, m, p, v, proc, score
            """ + RETURN_CLAUSE + """
            ORDER BY score DESC
            LIMIT $top_k
            """
            rows = _run_rows(cypher, {"fq": _lucene_query(keywords), "top_k": k})
            snippets = _normalize_rows(rows)
            method = "fulltext" if snippets else "none"
        except Exception:  # noqa: BLE001
            snippets = []

        # 路径 2：属性包含匹配（对中文更稳，全文索引未建或召回为空时启用）
        if not snippets:
            try:
                cypher = f"""
                MATCH (k:Knowledge)<-[:REPORTS]-(l:Literature)
                WHERE any(kw IN $keywords WHERE
                    toLower(k.statement) CONTAINS toLower(kw) OR
                    toLower(coalesce(k.evidence, '')) CONTAINS toLower(kw) OR
                    toLower(coalesce(l.title, '')) CONTAINS toLower(kw))
                WITH k, l, 1.0 AS score
                OPTIONAL MATCH (k)-[:ABOUT_MATERIAL]->(m:Material)
                OPTIONAL MATCH (k)-[:HAS_PROPERTY]->(p:Property)
                OPTIONAL MATCH (p)-[:HAS_VALUE]->(v:Value)
                OPTIONAL MATCH (m)-[:PROCESSED_BY]->(proc:Process)
                {RETURN_CLAUSE}
                ORDER BY score DESC
                LIMIT $top_k
                """
                rows = _run_rows(cypher, {"keywords": keywords, "top_k": k})
                snippets = _normalize_rows(rows)
                method = "contains" if snippets else "none"
            except Exception:  # noqa: BLE001
                snippets = []

    return {
        "question": question,
        "keywords": keywords,
        "snippets": snippets,
        "context": format_context(snippets),
        "method": method,
    }


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规整 Neo4j 返回的行，过滤空陈述并按 score 排序。"""
    result = []
    for row in rows:
        if not row.get("statement"):
            continue
        result.append(
            {
                "statement": row.get("statement", ""),
                "evidence": row.get("evidence") or "",
                "paper_id": row.get("paper_id") or "",
                "title": row.get("title") or "",
                "materials": _unique(row.get("materials")),
                "properties": _unique(row.get("properties")),
                "values": _unique(row.get("values")),
                "processes": _unique(row.get("processes")),
                "score": float(row.get("score") or 0),
            }
        )
    result.sort(key=lambda r: r["score"], reverse=True)
    return result


def _unique(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(str(value))
    return out


def format_context(snippets: list[dict[str, Any]]) -> str:
    """把召回的子图快照拼成带来源编号的结构化上下文。"""
    blocks = []
    for idx, item in enumerate(snippets, start=1):
        lines = [
            f"[来源 {idx}] {item['statement']}",
            f"  材料: {', '.join(item['materials']) or '未标注'} | "
            f"性能: {', '.join(item['properties']) or '未标注'} | "
            f"数值: {', '.join(item['values']) or '未标注'} | "
            f"工艺: {', '.join(item['processes']) or '未标注'}",
            f"  文献: {item['title']} (paper_id={item['paper_id']})",
        ]
        if item["evidence"]:
            lines.append(f"  原文证据: {item['evidence']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


ANSWER_SYSTEM_PROMPT = """你是材料科学文献助手，专门回答新能源汽车用铝合金等金属材料的问题。
严格依据提供的知识图谱上下文回答：
1. 每条事实后用 [来源 N] 标注出处，N 为上下文中给出的编号。
2. 不要使用上下文之外的常识补充，不要编造数值或文献。
3. 若上下文不足以回答问题，明确回答"根据当前文献库无法确定"，并说明缺少哪类信息。
4. 用简洁的中文分点作答，必要时引用具体数值、单位和工艺。"""


def _build_answer_messages(question: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"知识图谱上下文：\n{context}\n\n用户问题：{question}\n\n请给出回答。",
        },
    ]


def rag_query(question: str, top_k: int | None = None, include_context: bool = False) -> dict[str, Any]:
    """基于知识图谱回答问题。

    Args:
        question: 用户自然语言问题。
        top_k: 召回知识条数，默认取配置 TOP_K。
        include_context: 返回结果中是否附带原始 context（调试用）。

    Returns:
        {"question", "answer", "sources", "keywords", "retrieved", "method", ("context")}
    """
    retrieval = retrieve_context(question, top_k=top_k)
    sources = [
        {"index": i, "title": s["title"], "paper_id": s["paper_id"]}
        for i, s in enumerate(retrieval["snippets"], start=1)
    ]

    if not retrieval["snippets"]:
        result = {
            "question": question,
            "answer": "根据当前文献库无法确定：知识图谱中没有检索到与该问题相关的知识。"
                      "请先上传相关文献并完成知识构建，或换用更具体的材料牌号 / 性能指标提问。",
            "sources": [],
            "keywords": retrieval["keywords"],
            "retrieved": 0,
            "method": retrieval["method"],
        }
    else:
        client = get_llm_client()
        s = get_settings()
        response = client.chat.completions.create(
            model=s.llm_model,
            messages=_build_answer_messages(question, retrieval["context"]),
            temperature=s.temperature,
        )
        result = {
            "question": question,
            "answer": (response.choices[0].message.content or "").strip(),
            "sources": sources,
            "keywords": retrieval["keywords"],
            "retrieved": len(retrieval["snippets"]),
            "method": retrieval["method"],
        }

    if include_context:
        result["context"] = retrieval["context"]
    return result


def rag_query_text(question: str, top_k: int | None = None) -> str:
    """便捷函数：只返回答案文本（含来源标注）。"""
    return rag_query(question, top_k=top_k)["answer"]


def graph_stats() -> dict[str, int]:
    """统计图谱中各类型节点与关系数量，供界面展示。"""
    cypher = """
    MATCH (n)
    WITH labels(n) AS labels
    RETURN labels[0] AS label, count(*) AS count
    ORDER BY count DESC
    """
    rows = _run_rows(cypher, {})
    return {str(row["label"]): int(row["count"]) for row in rows if row.get("label")}
