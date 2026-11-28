"""离线自测：不连 Neo4j、不调用真实 LLM、不依赖真实 PDF，验证全链路逻辑。

运行：
    python examples/demo.py

用伪造的 Neo4j driver / LLM 客户端跑通：
    文献入库 → 分块抽取 → 写入图谱 → 检索 → 带来源的答案
并覆盖解析增强逻辑（图片引用、孤立页码、三线表重建、伪表格拒绝）。

真实 PDF 的表格 / 图片解析验证见 examples/check_parsing.py（会现场合成 PDF）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="mkg_selftest_"))
os.environ["PAPER_LIBRARY_DIR"] = str(TMP / "paper_library")
os.environ["NEO4J_PASSWORD"] = "fake-password"
os.environ["LLM_API_KEY"] = "fake-key"
os.environ["PDF2MD_SKILL_DIR"] = ""

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  -> {detail}" if detail else ""))


# ---------------- 伪造 Neo4j ----------------
class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def data(self):
        return self._rows


class _FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def run(self, cypher, **params):
        self.driver.calls.append((cypher.strip(), params))
        if "db.index.fulltext.queryNodes" in cypher or "WHERE any(kw" in cypher:
            return _FakeResult(self.driver.retrieval_rows)
        if "labels(n)" in cypher:
            return _FakeResult([{"label": "Knowledge", "count": 2}])
        return _FakeResult([])

    def execute_write(self, fn, *args):
        return fn(self, *args)


class _FakeDriver:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.retrieval_rows: list[dict] = []

    def session(self, database=None):
        return _FakeSession(self)

    def close(self):
        pass


# ---------------- 伪造 LLM ----------------
class _FakeLLM:
    def __init__(self):
        self.chat = self
        self.completions = self
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = "根据文献，6082 铝合金经 T6 时效后抗拉强度为 310 MPa [来源 1]。"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


SAMPLE_EXTRACTION = {
    "entities": [
        {"name": "6082", "type": "Material", "description": "铝合金牌号"},
        {"name": "抗拉强度", "type": "Property", "description": "力学性能指标"},
        {"name": "310 MPa", "type": "Value", "description": "带单位数值"},
        {"name": "T6时效", "type": "Process", "description": "热处理工艺"},
    ],
    "knowledge": [
        {
            "statement": "6082铝合金经T6时效后抗拉强度达到310 MPa",
            "evidence": "实验测得 6082-T6 试样抗拉强度为 310 MPa。",
            "materials": ["6082"],
            "properties": ["抗拉强度"],
            "values": ["310 MPa"],
            "processes": ["T6时效"],
        }
    ],
}

SAMPLE_MARKDOWN = """# 6082 铝合金 T6 时效性能研究

Table 2 Alloy composition of AA6082 and AA6086.

| Alloy | Mg | Si | Fe | Mn |
| --- | --- | --- | --- | --- |
| AA6082 | 0.6-1.2 | 0.7-1.3 | <0.5 | 0.4-1 |
| AA6086 | 1.01 | 1.58 | 0.18 | 0.71 |

![Figure 1](6082_t6_assets/fig1.png)

6082铝合金经T6时效后抗拉强度达到310 MPa。

1874
"""


def fake_extractor(chunk, paper_id, title, chunk_index):
    return SAMPLE_EXTRACTION


def main() -> int:
    print("=" * 68)
    print("materials-kg-rag 离线自测（无 Neo4j / 无 LLM / 无真实 PDF 依赖）")
    print("=" * 68)

    # 1. 导入
    print("\n[1] 模块导入")
    import config

    import kg_builder
    import pdf_parser
    import rag_query

    config.get_settings(refresh=True)
    check("五个核心模块可导入", True)
    public = config.get_settings().public_dict()
    check("配置脱敏", public["NEO4J_PASSWORD"] == "***" and public["LLM_API_KEY"] == "***")
    check("新增解析配置项可见",
          {"PDF_BACKEND", "PDF_TABLE_STRATEGY", "PDF_WRITE_IMAGES"} <= set(public))
    check("pdf2md 未配置时判定为不可用", config.get_settings().pdf2md_available is False)

    # 2. 文献入库（走文本分支，不需要 PDF 依赖）
    print("\n[2] 文献入库与论文库管理")
    source = TMP / "sample_source.md"
    source.write_text(SAMPLE_MARKDOWN, encoding="utf-8")
    markdown = pdf_parser.parse_pdf_to_markdown(str(source), paper_id="6082_t6")
    check("Markdown 含表格与数值", "| AA6082 | 0.6-1.2" in markdown and "310 MPa" in markdown)
    library = pdf_parser.list_paper_library()
    check("论文库列出已入库文献", any(p["paper_id"] == "6082_t6" for p in library))
    check("论文库条目含图片计数字段", all("images" in p for p in library))
    check("按 ID 读回内容一致", pdf_parser.load_paper("6082_t6") == markdown)
    edited = markdown + "\n\n## 校对补充\n人工确认数值无误。"
    pdf_parser.save_paper("6082_t6", edited)
    check("人工校对后覆盖入库", pdf_parser.load_paper("6082_t6") == edited)
    check("paper_id 清洗", pdf_parser.sanitize_paper_id("6082/T6:测试") == "6082_T6_测试")

    # 3. 解析增强逻辑（图片 / 页码 / 表格 / 三线表）
    print("\n[3] 解析增强逻辑：图片、页码、表格")
    refs = pdf_parser.normalize_image_refs(
        "![](C:/tmp/fig1.png)\n![Fig 1](D:\\work\\img_2.jpg)", "6082_t6_assets"
    )
    check("图片引用统一为相对路径", refs.count("6082_t6_assets/") == 2 and "C:/tmp" not in refs)

    noisy = "正文一段。\n\n1874\n\n| Alloy | Mg |\n| --- | --- |\n| 6082 | 1.0 |"
    cleaned = pdf_parser.strip_page_noise(noisy)
    check("孤立页码被清理且不误伤表格", "1874" not in cleaned and "| 6082 | 1.0 |" in cleaned)

    table_md = pdf_parser._table_to_markdown(
        [["Alloy", "Mg"], [None, None], ["AA6082", "0.6-1.2"], ["AA6086", "1.01"]]
    )
    check("表格过滤全空行", len(table_md.splitlines()) == 4, f"rows={len(table_md.splitlines())}")
    check("表格转义竖线", "a\\|b" in pdf_parser._table_to_markdown([["a|b", "c"], ["1", "2"]]))

    # 模拟三线表的 word 坐标：3 行 × 3 列，无框线
    words = [
        (50, 118, 85, 130, "Alloy", 0, 0, 0), (115, 118, 130, 130, "Mg", 0, 0, 0),
        (175, 118, 195, 130, "Si", 0, 0, 0),
        (50, 138, 95, 150, "AA6082", 0, 0, 0), (115, 138, 155, 150, "0.6-1.2", 0, 0, 0),
        (175, 138, 205, 150, "0.7-1.3", 0, 0, 0),
        (50, 154, 95, 166, "AA6086", 0, 0, 0), (115, 154, 145, 166, "1.01", 0, 0, 0),
        (175, 154, 200, 166, "1.58", 0, 0, 0),
    ]
    lines = pdf_parser._cluster_words_into_lines(words)
    check("词按 y 聚类成 3 行", len(lines) == 3, f"lines={len(lines)}")
    check("三行均判定为表格行", all(pdf_parser._row_is_table_like(line) for line in lines))
    anchor = max(lines, key=lambda line: len(line["words"]))
    centers = [w[0] for w in anchor["words"]]
    check("锚点行推断出 3 列", len(centers) == 3)
    check("所有行与列结构对齐", all(pdf_parser._line_matches_columns(line, centers) for line in lines))
    subtitle = {"y": 100.0, "words": [
        (50, 100, 90, 112, "Alloy", 0, 0, 0), (300, 100, 420, 112, "Majorelementwt%", 0, 0, 0)
    ]}
    check("跨列副标题被列结构拒绝", not pdf_parser._line_matches_columns(subtitle, centers))
    fake_table = (
        "| Table 2 Alloy | composition | of AA6082 | and AA608 | 6 [82,87]. |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Fig. 1. Microstr | ucture of the | AA6082 alloy. | | |\n"
    )
    check("整页伪表格被质量校验拒绝", not pdf_parser._table_lines_are_sane(fake_table))
    check("后端与表格策略参数校验",
          "auto" in pdf_parser.BACKENDS and "text" in pdf_parser.TABLE_STRATEGIES)

    # 4. 分块 / 配对 / 行构造
    print("\n[4] 分块 / 配对 / 行构造")
    chunks = kg_builder.split_markdown(edited, chunk_size=60, overlap=10)
    check("长文本被切分为多块", len(chunks) >= 2, f"chunks={len(chunks)}")
    pairs = kg_builder._pair_property_values(["抗拉强度", "延伸率"], ["310 MPa", "8.5 %"])
    check("属性-数值一一配对", pairs == [["抗拉强度", "310 MPa"], ["延伸率", "8.5 %"]])
    check("单属性多数值不产生笛卡尔积",
          len(kg_builder._pair_property_values(["抗拉强度"], ["310 MPa", "320 MPa"])) == 2)
    rows = kg_builder.build_rows_from_extraction(SAMPLE_EXTRACTION, "6082_t6", 0)
    check("抽取结果构造出知识行", len(rows) == 1 and rows[0]["statement"].startswith("6082"))
    check("知识行含稳定 uid", bool(rows[0]["uid"]) and rows[0]["uid"] ==
          kg_builder.build_rows_from_extraction(SAMPLE_EXTRACTION, "6082_t6", 0)[0]["uid"])
    check("本体 patterns 完整", len(kg_builder.PATTERNS) == 5)
    check("GraphRAG schema 用新 API 字段",
          set(kg_builder.GRAPHRAG_SCHEMA) >= {"node_types", "relationship_types", "patterns"})

    # 5. 注入假 driver，跑建图
    print("\n[5] 知识图谱构建（注入 extractor + 假 Neo4j）")
    fake_driver = _FakeDriver()
    fake_llm = _FakeLLM()
    kg_builder.get_driver = lambda: fake_driver
    rag_query.get_driver = lambda: fake_driver
    rag_query.get_llm_client = lambda: fake_llm

    stats = kg_builder.build_knowledge_graph(
        edited, "6082_t6", paper_title="6082铝合金T6时效性能研究",
        source_file="6082_t6.pdf", extractor=fake_extractor,
    )
    check("构建返回统计", stats["knowledge_nodes"] >= 1 and stats["entities"] == 4,
          f"chunks={stats['chunks']}, knowledge={stats['knowledge_nodes']}")
    check("抽取过程无错误", stats["errors"] == [], str(stats["errors"]))

    all_cypher = "\n".join(c for c, _ in fake_driver.calls)
    check("创建 Literature 节点", "MERGE (l:Literature" in all_cypher)
    check("创建 Knowledge 节点", "MERGE (k:Knowledge" in all_cypher)
    check("知识回溯文献 REPORTS", "[:REPORTS]" in all_cypher)
    check("建立材料关系 ABOUT_MATERIAL", "ABOUT_MATERIAL" in all_cypher)
    check("建立性能数值关系 HAS_VALUE", "HAS_VALUE" in all_cypher)
    check("建立工艺关系 PROCESSED_BY", "PROCESSED_BY" in all_cypher)
    # 兼容 Neo4j 5.x（CREATE FULLTEXT INDEX）与 3.5（db.index.fulltext.createNodeIndex）两种写法
    check("自动建立全文索引", "fulltext" in all_cypher.lower())
    check("写入使用幂等 MERGE", all_cypher.count("MERGE") >= 6)

    # 6. RAG
    print("\n[6] RAG 检索与问答")
    keywords = rag_query.extract_keywords("6082铝合金T6时效后的抗拉强度是多少？")
    check("关键词提取含牌号与性能", "6082" in keywords and "抗拉强度" in keywords, str(keywords))

    fake_driver.retrieval_rows = [
        {
            "statement": "6082铝合金经T6时效后抗拉强度达到310 MPa",
            "evidence": "实验测得 6082-T6 试样抗拉强度为 310 MPa。",
            "paper_id": "6082_t6",
            "title": "6082铝合金T6时效性能研究",
            "materials": ["6082"],
            "properties": ["抗拉强度"],
            "values": ["310 MPa"],
            "processes": ["T6时效"],
            "score": 1.0,
        }
    ]
    context = rag_query.retrieve_context("6082铝合金的抗拉强度是多少？", top_k=5)
    check("全文索引路径召回", context["method"] == "fulltext", context["method"])
    check("召回结构化上下文含 [来源 1]", "[来源 1]" in context["context"])
    check("上下文含材料/性能/数值/工艺",
          all(x in context["context"] for x in ["6082", "抗拉强度", "310 MPa", "T6时效"]))

    answer = rag_query.rag_query("6082-T6 的抗拉强度是多少？", top_k=5)
    check("答案带来源标注", "[来源 1]" in answer["answer"])
    check("返回文献来源清单", len(answer["sources"]) == 1 and
          answer["sources"][0]["paper_id"] == "6082_t6")
    check("LLM 被调用生成答案", len(fake_llm.calls) >= 1)

    fake_driver.retrieval_rows = []
    empty_answer = rag_query.rag_query("完全不相关的量子引力问题", top_k=5)
    check("无召回时给出确定性兜底，不编造",
          "无法确定" in empty_answer["answer"] and empty_answer["sources"] == [])

    # 7. 工具自动发现
    print("\n[7] Agent 工具注册接口")
    spec = importlib.util.spec_from_file_location(
        "materials_kg_skill", ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["materials_kg_skill"] = pkg
    spec.loader.exec_module(pkg)
    tools = pkg.get_tools()
    check("get_tools() 返回可调用函数", len(tools) >= 5 and all(callable(t) for t in tools),
          f"tools={len(tools)}")
    check("TOOL_SPECS 含必需字段",
          all({"name", "description", "parameters", "required"} <= set(s) for s in pkg.TOOL_SPECS))

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n" + "=" * 68)
    print(f"结果：{passed}/{total} 项通过")
    print("=" * 68)
    if passed != total:
        print("失败项：")
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  - {name} {detail}")
        return 1
    print("全部通过 ✅  真实 PDF 的表格/图片验证请运行 examples/check_parsing.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
