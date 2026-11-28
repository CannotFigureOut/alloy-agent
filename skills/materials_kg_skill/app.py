"""材料文献知识图谱工作台（本地调试 / 汇报演示用，不部署公网）。

运行：
    streamlit run app.py

三个 Tab：
  1. 上传解析：上传 PDF → 转 Markdown → 人工校对 → 构建知识图谱
  2. 论文库：查看 / 下载已入库文献的 Markdown
  3. 知识图谱：图谱统计、pyvis 子图可视化、RAG 问答
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

# 兼容两种运行方式：streamlit run app.py（脚本）与 import materials_kg_skill.app（包）
try:
    from .config import get_driver, get_settings
    from .kg_builder import build_knowledge_graph, ensure_indexes
    from .pdf_parser import (
        list_paper_images,
        list_paper_library,
        load_paper,
        parse_pdf_to_markdown,
        save_paper,
    )
    from .rag_query import graph_stats, rag_query, retrieve_context
except ImportError:  # pragma: no cover
    from config import get_driver, get_settings
    from kg_builder import build_knowledge_graph, ensure_indexes
    from pdf_parser import (
        list_paper_images,
        list_paper_library,
        load_paper,
        parse_pdf_to_markdown,
        save_paper,
    )
    from rag_query import graph_stats, rag_query, retrieve_context

NODE_COLORS = {
    "Literature": "#ef5350",
    "Knowledge": "#42a5f5",
    "Material": "#66bb6a",
    "Property": "#ffa726",
    "Value": "#ab47bc",
    "Process": "#26c6da",
}


# ------------------------------ 辅助函数 ------------------------------

def graph_stats_safe() -> dict[str, int]:
    """图谱节点统计，异常时返回空字典而不是中断页面。"""
    try:
        return graph_stats()
    except Exception:  # noqa: BLE001
        return {}


def render_subgraph(paper_id: str | None, limit: int = 120) -> str:
    """查询子图并用 pyvis 生成 HTML 片段；无数据返回空字符串。"""
    from pyvis.network import Network

    params: dict[str, object] = {"limit": limit}
    if paper_id:
        cypher = """
        MATCH (l:Literature {paper_id: $paper_id})-[:REPORTS]->(k:Knowledge)
        MATCH (k)-[r]->(b)
        RETURN id(l) AS aid, 'Literature' AS alabel, l.title AS aname,
               id(b) AS bid, labels(b)[0] AS blabel,
               coalesce(b.name, b.statement, b.paper_id, b.uid) AS bname,
               type(r) AS rel
        LIMIT $limit
        """
        params["paper_id"] = paper_id
    else:
        cypher = """
        MATCH (a)-[r]->(b)
        WHERE (a:Knowledge OR b:Knowledge OR a:Literature)
        RETURN id(a) AS aid, labels(a)[0] AS alabel,
               coalesce(a.name, a.statement, a.paper_id, a.uid) AS aname,
               id(b) AS bid, labels(b)[0] AS blabel,
               coalesce(b.name, b.statement, b.paper_id, b.uid) AS bname,
               type(r) AS rel
        LIMIT $limit
        """

    with get_driver().session(database=get_settings().neo4j_database) as session:
        rows = session.run(cypher, **params).data()
    if not rows:
        return ""

    net = Network(height="500px", width="100%", bgcolor="#15171c", font_color="#e8eaf0", directed=True)
    net.set_options("""
    { "physics": { "stabilization": {"iterations": 120},
                   "barnesHut": {"gravitationalConstant": -12000} },
      "nodes": {"shape": "dot", "size": 16, "font": {"size": 13}},
      "edges": {"font": {"size": 10, "align": "middle"}, "arrows": {"to": {"enabled": true}}} }
    """)
    added: set[str] = set()
    for row in rows:
        for key, label_key in (("aid", "alabel"), ("bid", "blabel")):
            nid = str(row[key])
            if nid in added:
                continue
            label = row[label_key] or "Node"
            name = str(row["aname" if key == "aid" else "bname"] or label)
            net.add_node(nid, label=name[:40], title=f"{label}: {name}",
                         color=NODE_COLORS.get(label, "#9e9e9e"))
            added.add(nid)
        net.add_edge(str(row["aid"]), str(row["bid"]), title=row["rel"], label=row["rel"])

    out_path = Path(tempfile.gettempdir()) / "materials_kg_subgraph.html"
    net.save_graph(str(out_path))
    return out_path.read_text(encoding="utf-8")


# ------------------------------ 页面 ------------------------------

st.set_page_config(page_title="材料文献知识图谱工作台", page_icon="🧪", layout="wide")
st.title("🧪 材料文献智能解析与知识构建工作台")
st.caption("Skill: materials-kg-rag · PDF → Markdown → Neo4j 知识图谱 → RAG 问答")

cfg = get_settings()
papers = list_paper_library()

# ------------------------------ 侧边栏：配置状态 ------------------------------
with st.sidebar:
    st.header("⚙️ 运行配置")
    st.json(cfg.public_dict())
    if not cfg.neo4j_configured:
        st.warning("Neo4j 未配置：图谱构建与问答不可用。请复制 .env.example 为 .env 并填写密码。")
    if not cfg.llm_configured:
        st.warning("LLM 未配置：知识抽取与答案生成不可用。请填写 LLM_API_KEY。")
    st.caption("PDF 方案 A：" + ("pdf2md-agent-skill 可用" if cfg.pdf2md_available else "未配置"))
    try:
        import pymupdf4llm  # noqa: F401

        st.caption("PDF 方案 B：pymupdf4llm 已安装 ✅")
    except ImportError:
        st.caption("PDF 方案 B：未安装 pymupdf4llm → 自动使用纯 PyMuPDF 增强方案 C")
    st.caption(f"解析后端：{cfg.pdf_backend} · 表格策略：{cfg.pdf_table_strategy}")
    if st.button("建立 / 刷新 Neo4j 索引"):
        try:
            ensure_indexes()
            st.success("索引与约束已就绪")
        except Exception as exc:  # noqa: BLE001
            st.error(f"索引创建失败：{exc}")

tab_upload, tab_library, tab_graph = st.tabs(["📥 上传解析", "📚 论文库", "🕸️ 知识图谱 & 问答"])

# ------------------------------ Tab 1：上传解析 ------------------------------
with tab_upload:
    st.subheader("1. 上传并解析文献")
    uploaded = st.file_uploader("选择 PDF / Markdown 文件", type=["pdf", "md", "txt", "markdown"])
    col_a, col_b = st.columns([1, 2])
    with col_a:
        language = st.selectbox("文献语言", ["auto", "zh", "en"], index=0,
                                help="auto 自动判断；zh 会调用中文 OCR 管线")
        backend = st.selectbox(
            "解析后端",
            ["auto", "pymupdf4llm", "pymupdf_enhanced", "pdf2md_skill"],
            index=0,
            help="auto 自动降级：外部 Skill → 官方 pymupdf4llm → 纯 PyMuPDF 增强",
        )
        table_strategy = st.selectbox(
            "表格检测策略",
            ["lines_strict", "lines", "text", "explicit"],
            index=0,
            help="⚠ 三线表/无框线表格（学术论文常见）请选 text：用文字坐标推断列边界",
        )
        write_images = st.checkbox("导出 PDF 图片", value=True,
                                   help="图片保存到 <paper_id>_assets/ 并在 Markdown 中引用")
    with col_b:
        st.caption(
            "解析后端会自动降级，任一路径失败都不影响流水线。\n\n"
            "**表格仍是压扁的纯文本？** 把「表格检测策略」改成 `text`。\n\n"
            "**图片没出来？** 勾选「导出 PDF 图片」，重新解析即可。"
        )

    if uploaded:
        tmp_dir = Path(tempfile.gettempdir()) / "materials_kg_skill_uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / uploaded.name
        tmp_path.write_bytes(uploaded.getbuffer())
        default_pid = Path(uploaded.name).stem

        if st.button("① 解析为 Markdown", type="primary"):
            with st.spinner("正在解析文献…"):
                try:
                    md = parse_pdf_to_markdown(
                        str(tmp_path),
                        language=language,
                        paper_id=default_pid,
                        backend=backend,
                        table_strategy=table_strategy,
                        write_images=write_images,
                    )
                    st.session_state["draft_markdown"] = md
                    st.session_state["draft_pid"] = default_pid
                    image_count = len(list_paper_images(default_pid))
                    st.success(
                        f"解析完成，共 {len(md)} 字符，已暂存到论文库"
                        + (f" · 导出 {image_count} 张图片" if image_count else "")
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"解析失败：{exc}")

        pid = st.text_input("文献 ID（入库文件名）", value=st.session_state.get("draft_pid", default_pid))
        title = st.text_input("文献标题", value=default_pid)
        draft = st.text_area(
            "Markdown 内容（可人工校对后再入库）",
            value=st.session_state.get("draft_markdown", ""),
            height=380,
        )

        if st.button("② 确认入库并构建知识图谱", type="primary"):
            if not draft.strip():
                st.warning("Markdown 为空，请先完成第 ① 步解析。")
            else:
                with st.spinner("正在抽取三元组并写入 Neo4j…"):
                    try:
                        save_paper(pid, draft)
                        stats = build_knowledge_graph(draft, pid, title, source_file=uploaded.name)
                        st.success("知识图谱构建完成")
                        st.json(stats)
                        if stats.get("errors"):
                            st.warning(f"有 {len(stats['errors'])} 个分块抽取失败，详见上方 errors。")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"构建失败：{exc}")
    else:
        st.info("请先上传一篇 PDF 文献开始。")

# ------------------------------ Tab 2：论文库 ------------------------------
with tab_library:
    st.subheader("2. 已入库论文库")
    if not papers:
        st.info("论文库为空。请先在「上传解析」Tab 中解析并入库文献。")
    else:
        st.caption(f"共 {len(papers)} 篇 · 目录：{cfg.paper_library_dir}")
        labels = [f"{p['paper_id']}  ({p['size_kb']} KB)" for p in papers]
        selected = st.selectbox("选择文献", labels)
        chosen = papers[labels.index(selected)]
        content = load_paper(chosen["paper_id"])
        st.download_button("⬇️ 下载 Markdown", content, file_name=f"{chosen['paper_id']}.md")
        st.caption(
            f"Markdown 表格：{content.count('| --- |')} 处 · "
            f"图片引用：{content.count('![ ')} / {content.count('![')} · "
            f"已导出图片：{chosen.get('images', 0)} 张"
        )
        images = list_paper_images(chosen["paper_id"])
        if images:
            with st.expander(f"🖼️ 查看导出的 {len(images)} 张图片（校对用）"):
                cols = st.columns(3)
                for idx, img in enumerate(images[:12]):
                    with cols[idx % 3]:
                        st.image(img, use_column_width=True, caption=Path(img).name)
        st.text_area("Markdown 原文（只读）", content, height=460, disabled=True)

# ------------------------------ Tab 3：知识图谱 & 问答 ------------------------------
with tab_graph:
    st.subheader("3. 图谱概览")
    stats = graph_stats_safe()
    if stats:
        st.write(" | ".join(f"**{k}**: {v}" for k, v in stats.items()))
    else:
        st.info("图谱为空或 Neo4j 未连接。")

    st.divider()
    st.subheader("子图可视化")
    target = st.selectbox("选择要展示的文献子图", ["（全部）"] + [p["paper_id"] for p in papers])
    if st.button("绘制子图"):
        try:
            html = render_subgraph(None if target == "（全部）" else target)
            if html:
                import streamlit.components.v1 as components

                components.html(html, height=520, scrolling=True)
            else:
                st.info("没有可展示的子图数据。")
        except Exception as exc:  # noqa: BLE001
            st.error(f"绘制失败（是否已安装 pyvis？）：{exc}")

    st.divider()
    st.subheader("RAG 问答")
    question = st.text_input("输入关于材料性能的问题",
                             placeholder="例如：6082 铝合金 T6 时效后的抗拉强度是多少？")
    top_k = st.slider("召回知识条数", 1, 15, 5)
    if st.button("提问", type="primary") and question.strip():
        with st.spinner("检索知识图谱并生成答案…"):
            try:
                result = rag_query(question, top_k=top_k)
                st.markdown("**回答**")
                st.markdown(result["answer"])
                st.caption(
                    f"召回 {result['retrieved']} 条知识 · 检索方式 {result['method']} "
                    f"· 关键词：{', '.join(result['keywords'])}"
                )
                if result["sources"]:
                    with st.expander("📖 文献来源"):
                        for src in result["sources"]:
                            st.write(f"[来源 {src['index']}] {src['title']}  (`{src['paper_id']}`)")
                with st.expander("🔍 查看检索上下文"):
                    context = retrieve_context(question, top_k=top_k)["context"]
                    st.text(context or "（无）")
            except Exception as exc:  # noqa: BLE001
                st.error(f"问答失败：{exc}")
