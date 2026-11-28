"""materials-kg-rag —— 材料文献知识图谱构建与 RAG 问答 Skill。

供外部 Agent 框架直接 import 的能力包。子模块按需加载，避免可选依赖缺失时
导致整个包无法导入。
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["config", "pdf_parser", "kg_builder", "rag_query", "get_tools", "TOOL_SPECS"]


def get_tools() -> list:
    """返回可被 Agent 框架注册的工具函数列表。

    用法：
        from materials_kg_skill import get_tools
        for fn in get_tools():
            framework.register(fn)
    """
    from . import kg_builder, pdf_parser, rag_query

    return [
        pdf_parser.parse_pdf_to_markdown,
        kg_builder.build_knowledge_graph,
        kg_builder.build_from_pdf_graphrag,
        rag_query.retrieve_context,
        rag_query.rag_query,
    ]


# 轻量工具描述，便于基于 JSON Schema 的框架生成 tool definition。
TOOL_SPECS: list[dict] = [
    {
        "name": "parse_pdf_to_markdown",
        "description": "把材料科学 PDF 文献转换为 Markdown 并存入论文库，返回 Markdown 文本。",
        "parameters": {
            "pdf_path": {"type": "string", "description": "PDF 文件的本地绝对路径"},
            "language": {
                "type": "string",
                "enum": ["auto", "zh", "en"],
                "description": "文献正文语言，auto 表示自动判断",
            },
        },
        "required": ["pdf_path"],
    },
    {
        "name": "build_knowledge_graph",
        "description": "从 Markdown 抽取材料知识三元组写入 Neo4j，每条知识回溯到文献节点。",
        "parameters": {
            "markdown_text": {"type": "string", "description": "文献的 Markdown 全文"},
            "paper_id": {"type": "string", "description": "文献唯一标识，建议用文件名或 DOI 哈希"},
            "paper_title": {"type": "string", "description": "文献标题（可选）"},
        },
        "required": ["markdown_text", "paper_id"],
    },
    {
        "name": "rag_query",
        "description": "基于知识图谱回答关于材料性能的问题，返回答案与文献来源。",
        "parameters": {
            "question": {"type": "string", "description": "用户自然语言问题"},
            "top_k": {"type": "integer", "description": "召回的知识条数，默认 5"},
        },
        "required": ["question"],
    },
]
