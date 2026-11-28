"""PDF / 文档 → Markdown（文献解析工作台后端）。

解析后端（auto 模式按质量从高到低自动降级）：
  A  pdf2md-agent-skill : 外部 Skill，公式 LaTeX / 双栏排序 / OCR / 图片导出。需 PDF2MD_SKILL_DIR。
  B  pymupdf4llm        : PyMuPDF 官方开源方案。write_images 导出图片 + table_strategy 表格检测。
  C  pymupdf-enhanced   : 纯 PyMuPDF 自研增强。多策略表格检测 + 图片导出 + bbox 阅读顺序 + 页眉页脚清理。

借鉴的开源实现（见 README「解析后端与开源借鉴」）：
  - pymupdf/RAG (PyMuPDF4LLM)：write_images / image_path / table_strategy / page_chunks / margins
  - Future-House/paper-qa PR #1329：三线表(booktabs)被 find_tables() 默认策略漏检的原因与
    caption 锚定 + 文本对齐检测思路
  - HuggingFace Agentic_RAG_PDF_QA / utils/layout_extractor.py：用表格 bbox 屏蔽文本块、
    在正确位置原位插入 Markdown 表格
  - taotaoboom/pdf2md-agent-skill：跳过覆盖整页的扫描底图、过滤页眉页脚
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:  # 包内导入 / 顶层模块导入 双兼容
    from .config import get_settings
except ImportError:  # pragma: no cover
    from config import get_settings

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
PDF_SUFFIXES = {".pdf"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff"}
BACKENDS = {"auto", "pdf2md_skill", "pymupdf4llm", "pymupdf_enhanced"}
TABLE_STRATEGIES = {"lines", "lines_strict", "text", "explicit"}


# ------------------------------ 通用工具 ------------------------------

def sanitize_paper_id(name: str) -> str:
    """把文件名 / DOI 清洗成安全的 paper_id（用作文件名与 Neo4j 属性值）。"""
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip(), flags=re.UNICODE)
    return cleaned.strip("._-") or "paper"


def detect_language(pdf_path: str | Path, max_chars: int = 3000) -> str:
    """读取前两页文本判断正文语言，返回 'zh' 或 'en'。读不出时返回 'zh'。"""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        text = ""
        for i in range(min(2, len(doc))):
            text += doc[i].get_text()
            if len(text) >= max_chars:
                break
        doc.close()
    except Exception:  # noqa: BLE001
        return "zh"
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z]", text))
    return "zh" if zh + en > 0 and zh / (zh + en) > 0.15 else "en"


def assets_dir_for(library_dir: Path, paper_id: str) -> Path:
    """图片资源目录：<library>/<paper_id>_assets/，与 Markdown 同级，便于一起分发。"""
    return library_dir / f"{paper_id}_assets"


# ------------------------------ Markdown 后处理 ------------------------------

_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_NOISE_LINE = re.compile(r"^\s*(?:\d{1,4}|(?:Page\s+)?\d{1,4}\s*(?:/|of)\s*\d{1,4})\s*$", re.I)
_TABLE_CAPTION_RE = re.compile(r"(?im)^\s*(?:Table|表)\s*\d+\b")


def normalize_image_refs(markdown: str, assets_name: str) -> str:
    """把图片引用统一改成相对路径 <assets_name>/<文件名>，便于 md 与图片一起上传 GitHub。"""

    def repl(match: re.Match) -> str:
        alt = match.group(1)
        raw = match.group(2).strip().strip('"').replace("\\", "/").split("?")[0]
        fname = Path(raw).name
        if not fname:
            return match.group(0)
        return f"![{alt}]({assets_name}/{fname})"

    return _IMG_RE.sub(repl, markdown)


def strip_page_noise(markdown: str) -> str:
    """清理孤立页码 / 页眉页脚，并压缩多余空行。

    表格行形如 ``| 310 | ... |``，不会被整行纯数字规则误伤。
    """
    lines = [ln for ln in markdown.splitlines() if not _NOISE_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _count_table_rows(markdown: str) -> int:
    """统计 Markdown 表格的行数（连续的 | ... | 行）。"""
    return sum(1 for ln in markdown.splitlines() if ln.strip().startswith("|") and ln.strip().endswith("|"))


def _has_table_caption(markdown: str) -> bool:
    return bool(_TABLE_CAPTION_RE.search(markdown))


def _table_lines_are_sane(markdown: str) -> bool:
    """拒绝 text 策略把整页正文（含标题 / 图注）误判成巨大伪表格的结果。"""
    rows = [ln.strip() for ln in markdown.splitlines()
            if ln.strip().startswith("|") and ln.strip().endswith("|")]
    if len(rows) < 2 or len(rows) > 60:
        return False
    for line in rows[:3]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if any(len(c) > 60 for c in cells):
            return False
        if any(re.match(r"(?i)^(table|fig(?:ure)?\.?)\s*\d", c) for c in cells):
            return False
    return True


def _bbox_iou(a, b) -> float:
    """两个矩形的 IoU，用于表格去重。a/b 为类 Rect 对象（有 x0,y0,x1,y1 与 intersect 方法）。"""
    try:
        inter = a & b
        ai = max(0.0, (a.x1 - a.x0) * (a.y1 - a.y0))
        bi = max(0.0, (b.x1 - b.x0) * (b.y1 - b.y0))
        ii = max(0.0, (inter.x1 - inter.x0) * (inter.y1 - inter.y0))
        union = ai + bi - ii
        return ii / union if union > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _cover_ratio(inner, outer) -> float:
    """inner 有多少比例落在 outer 内，用于判断文本块是否属于某个表格区域。"""
    try:
        inter = inner & outer
        ia = max(0.0, (inner.x1 - inner.x0) * (inner.y1 - inner.y0))
        ii = max(0.0, (inter.x1 - inter.x0) * (inter.y1 - inter.y0))
        return ii / ia if ia > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# ------------------------------ 方案 A：pdf2md-agent-skill ------------------------------

def _convert_with_pdf2md_skill(pdf_path: Path, out_path: Path, language: str, timeout: int = 900) -> str:
    """调用外部 pdf2md-agent-skill 脚本，返回生成的 Markdown 文本。"""
    s = get_settings()
    if not s.pdf2md_skill_dir:
        raise RuntimeError("未配置 PDF2MD_SKILL_DIR")
    scripts = s.pdf2md_skill_dir / "scripts"
    preferred = "md_convert_zh.py" if language == "zh" else "md_convert.py"
    script = scripts / preferred
    if not script.exists():
        script = scripts / "md_convert.py"
    if not script.exists():
        raise FileNotFoundError(f"在 {scripts} 中找不到 md_convert.py / md_convert_zh.py")

    cmd = [sys.executable, str(script), str(pdf_path), "-o", str(out_path)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(s.pdf2md_skill_dir),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"pdf2md-agent-skill 转换失败（exit {proc.returncode}）：{detail}")
    if not out_path.exists():
        raise RuntimeError("pdf2md-agent-skill 未生成输出文件")
    return out_path.read_text(encoding="utf-8", errors="replace")


# ------------------------------ 方案 B：PyMuPDF 官方 pymupdf4llm ------------------------------

def _import_pymupdf4llm():
    """pymupdf4llm 已并入 pymupdf 2.x，兼容两种导入路径。"""
    try:
        import pymupdf4llm

        return pymupdf4llm
    except ImportError:
        pass
    try:
        from pymupdf import pymupdf4llm as module

        return module
    except ImportError:
        pass
    raise RuntimeError("未安装 pymupdf4llm，请运行 pip install pymupdf4llm")


def _retry_page_with_text_strategy(module, pdf_path: Path, page_index: int,
                                   original: str, common: dict) -> str:
    """三线表兜底：某页有 Table 标题但 lines_strict 没找到表时，改用 text 策略重跑该页。

    借鉴 paper-qa PR #1329：booktabs 三线表没有竖线，线条策略必然漏检，需要基于文本位置
    推断虚拟列边界。只在检测到确实生成了表格时才替换，避免 text 策略把正文误判成表格。
    """
    try:
        kwargs = {**common, "table_strategy": "text"}
        retry = module.to_markdown(str(pdf_path), pages=[page_index], **kwargs)
        candidate = retry[0].get("text", "") if isinstance(retry, list) and retry else str(retry)
    except Exception:  # noqa: BLE001
        return original
    if _count_table_rows(candidate) < 2 or not _table_lines_are_sane(candidate):
        return original
    refs = re.findall(r"!\[[^\]]*\]\([^)]+\)", original)
    if refs and "![" not in candidate:
        candidate = candidate + "\n\n" + "\n\n".join(refs)
    return candidate


def _convert_with_pymupdf4llm(
    pdf_path: Path,
    out_path: Path,
    assets_dir: Path,
    table_strategy: str,
    write_images: bool,
    dpi: int,
) -> str:
    """方案 B：PyMuPDF 官方 pymupdf4llm。图片导出 + 表格检测 + 页眉页脚过滤。"""
    module = _import_pymupdf4llm()
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets_name = assets_dir.name

    common = {
        "page_chunks": True,
        "write_images": write_images,
        "image_path": str(assets_dir),
        "image_format": "png",
        "dpi": dpi,
        "show_progress": False,
        # margins 保持默认 (0, 50, 0, 50)：自动忽略页眉页脚区域，孤立页码通常被一并过滤
    }
    chunks = module.to_markdown(str(pdf_path), table_strategy=table_strategy, **common)
    if isinstance(chunks, str):
        chunks = [{"text": chunks, "tables": []}]

    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
        tables_meta = (chunk.get("tables") or []) if isinstance(chunk, dict) else []

        # 三线表兜底：有 Table 标题却没有检测到表格 → 用 text 策略重跑本页
        if _has_table_caption(text) and not tables_meta and table_strategy != "text":
            text = _retry_page_with_text_strategy(module, pdf_path, index, text, common)

        text = normalize_image_refs(text, assets_name)
        text = strip_page_noise(text)
        parts.append(f"## Page {index + 1}\n\n{text}")

    markdown = "\n\n".join(parts)
    out_path.write_text(markdown, encoding="utf-8")
    return markdown


# ------------------------------ 方案 C：纯 PyMuPDF 自研增强 ------------------------------

_HEADING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+(.{2,90})$")
_LIST_RE = re.compile(r"^\s*(?:[-*•]|\(?\d{1,2}[.)])\s+(.*)$")


def _format_block(text: str) -> str:
    """把一个文本块整理成 Markdown 段落 / 标题 / 列表。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append(" ".join(buffer))
            buffer.clear()

    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading and not line.endswith(("。", ".", "，", ",")):
            flush()
            depth = heading.group(1).count(".") + 1
            out.append("#" * min(depth + 1, 6) + " " + line)
            continue
        item = _LIST_RE.match(line)
        if item:
            flush()
            out.append("- " + item.group(1).strip())
            continue
        buffer.append(line)
    flush()
    return "\n\n".join(out)


def _table_to_markdown(rows: list[list[str | None]]) -> str:
    """把抽取出的二维表格转成 GitHub-Flavored Markdown 表格（借鉴 layout_extractor.py）。

    过滤掉全空行：三线表的横线常被 text 策略识别成空数据行。
    """
    if not rows:
        return ""

    def clean(cell) -> str:
        if cell is None:
            return ""
        return str(cell).replace("\n", " ").replace("|", r"\|").strip()

    cleaned = [[clean(cell) for cell in row] for row in rows]
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    cleaned = [row for row in cleaned if any(cell for cell in row)]  # 去掉全空行
    if len(cleaned) < 2:
        return ""
    header, body = cleaned[0], cleaned[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _md_table_has_content(md: str) -> bool:
    rows = [ln for ln in md.splitlines() if ln.strip().startswith("|")]
    if len(rows) < 3:  # 表头 + 分隔行 + 至少 1 行数据
        return False
    return any(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", ln.replace("|", "").replace("-", "")) for ln in rows[2:])


_CAPTION_BLOCK_RE = re.compile(r"(?i)^\s*(?:Table|表)\s*\d+[\.\s:]")


def _find_caption_rects(page, fitz) -> list:
    """定位页面上的 'Table N' / '表 N' 标题块，作为三线表的锚点。

    借鉴 paper-qa PR #1329 的 caption anchoring 策略：从标题向下扩展出表格区域。
    """
    rects = []
    for block in page.get_text("dict")["blocks"]:
        if not isinstance(block, dict) or block.get("type") != 0 or "bbox" not in block:
            continue
        text = _block_text(block).strip()
        if _CAPTION_BLOCK_RE.match(text) and len(text) < 200:
            rects.append(fitz.Rect(block["bbox"]))
    return rects


def _is_credible_table(md: str, rect, page_rect) -> bool:
    """质量校验，拒绝 text 策略把整页正文误判成表格的结果。"""
    rows = [ln for ln in md.splitlines() if ln.strip().startswith("|") and ln.strip().endswith("|")]
    if len(rows) < 3 or len(rows) > 60:
        return False
    area = max(0.0, rect.x1 - rect.x0) * max(0.0, rect.y1 - rect.y0)
    page_area = max(1.0, page_rect.width * page_rect.height)
    if area > page_area * 0.6:
        return False
    for line in rows:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > 25:
            return False
        if any(len(c) > 60 for c in cells):  # 混入长句 / caption
            return False
        if any(re.match(r"(?i)^(table|fig(?:ure)?\.?)\s*\d", c) for c in cells):
            return False
    return True


def _cluster_words_into_lines(words, tol: float = 4.0) -> list[dict]:
    """把 get_text('words') 的结果按 y 坐标聚类成视觉行。"""
    lines: list[dict] = []
    for w in sorted(words, key=lambda item: (item[1], item[0])):
        for line in lines:
            if abs(line["y"] - w[1]) <= tol:
                line["words"].append(w)
                line["y"] = sum(x[1] for x in line["words"]) / len(line["words"])
                break
        else:
            lines.append({"y": float(w[1]), "words": [w]})
    for line in lines:
        line["words"].sort(key=lambda item: item[0])
    lines.sort(key=lambda item: item["y"])
    return lines


def _row_is_table_like(line: dict) -> bool:
    """判断一行是否像表格行：多个短 token，不是长句 / 图注 / 标题。"""
    words = line["words"]
    if not 2 <= len(words) <= 25:
        return False
    texts = [w[4] for w in words]
    if any(len(t) > 28 for t in texts):
        return False
    joined = " ".join(texts)
    if re.match(r"(?i)^\s*(fig(?:ure)?\.?|table)\s*\d", joined):
        return False
    short = sum(1 for t in texts if len(t) <= 14)
    return short / len(texts) >= 0.6


def _infer_column_centers(lines: list[dict], tol: float = 12.0) -> list[float]:
    """对所有词左边界做一维聚类，推断表格列中心。"""
    xs = sorted(w[0] for line in lines for w in line["words"])
    if not xs:
        return []
    clusters: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [sum(c) / len(c) for c in clusters]


def _line_matches_columns(line: dict, centers: list[float],
                          tol: float = 12.0, min_ratio: float = 0.6) -> bool:
    """判断一行是否与推断出的列结构对齐（用于剔除副标题、图注等干扰行）。"""
    words = line["words"]
    if len(words) < max(2, int(len(centers) * 0.6)):
        return False
    matched = sum(1 for w in words if min(abs(c - w[0]) for c in centers) <= tol)
    return matched / len(words) >= min_ratio


def _rebuild_borderless_table(page, fitz, caption_rect, max_down: float = 320.0):
    """按词坐标对齐重建三线表 / 无框线表，返回 (bbox, markdown) 或 None。

    借鉴 Future-House/paper-qa PR #1329 的 text-alignment runs 策略：不依赖
    线条交点，而是用连续文本行的 x 对齐关系推断列，因此能处理 booktabs 三线表。

    以「词数最多的行」为锚点确定列边界，再向上下扩展只保留与列结构对齐的行，
    从而排除 'Alloy Major element (wt.%)' 这类跨列副标题。
    """
    try:
        words = [w for w in page.get_text("words")
                 if caption_rect.y1 + 1 < w[1] < caption_rect.y1 + max_down]
    except Exception:  # noqa: BLE001
        return None
    if len(words) < 4:
        return None

    lines = _cluster_words_into_lines(words)
    candidates = [line for line in lines if _row_is_table_like(line)]
    if len(candidates) < 2:
        return None

    anchor = max(candidates, key=lambda line: len(line["words"]))
    if len(anchor["words"]) < 3:
        return None
    centers = [w[0] for w in anchor["words"]]  # 锚点行的词左边界即列中心
    if not 2 <= len(centers) <= 20:
        return None

    anchor_idx = next(i for i, line in enumerate(candidates) if line is anchor)
    selected = [anchor]
    for line in candidates[anchor_idx + 1:]:
        if _line_matches_columns(line, centers):
            selected.append(line)
        else:
            break
    for line in reversed(candidates[:anchor_idx]):
        if _line_matches_columns(line, centers):
            selected.insert(0, line)
        else:
            break
    if len(selected) < 2:
        return None

    rows = []
    for line in selected:
        cells = [""] * len(centers)
        for w in line["words"]:
            idx = min(range(len(centers)), key=lambda i: abs(centers[i] - w[0]))
            if abs(centers[idx] - w[0]) > 32:
                continue
            cells[idx] = (cells[idx] + " " + w[4]).strip() if cells[idx] else w[4]
        rows.append(cells)

    md = _table_to_markdown(rows)
    if not _md_table_has_content(md):
        return None
    all_words = [w for line in selected for w in line["words"]]
    rect = fitz.Rect(
        min(w[0] for w in all_words), min(w[1] for w in all_words),
        max(w[2] for w in all_words), max(w[3] for w in all_words),
    )
    return rect, md


def _detect_tables(page, fitz, primary_strategy: str) -> list[tuple]:
    """检测页面表格：线条策略为主 + caption 锚定的三线表受控兜底。

    关键教训（已用真实 PDF 验证）：直接对整页使用 strategy="text" 会把标题、
    图注一起吞成一张巨大的伪表格。因此 text 策略必须在 caption 下方用 clip
    限定区域后使用，并经过 _is_credible_table 质量校验。
    """
    found: list[tuple] = []
    seen: list = []

    # 1) 有框线表格：只信任线条类策略（默认 lines_strict，更抗背景色干扰）
    line_strategies = [s for s in dict.fromkeys([primary_strategy, "lines"])
                       if s in {"lines", "lines_strict"}]
    for strategy in line_strategies or ["lines_strict"]:
        try:
            finder = page.find_tables(strategy=strategy)
        except TypeError:  # 老版本 PyMuPDF 不支持 strategy 参数
            try:
                finder = page.find_tables()
            except Exception:  # noqa: BLE001
                continue
        except Exception:  # noqa: BLE001
            continue
        for table in getattr(finder, "tables", []):
            rect = fitz.Rect(table.bbox)
            if any(_bbox_iou(rect, s) > 0.5 for s in seen):
                continue
            try:
                md = _table_to_markdown(table.extract())
            except Exception:  # noqa: BLE001
                md = ""
            if _md_table_has_content(md) and _is_credible_table(md, rect, page.rect):
                found.append((rect, md))
                seen.append(rect)

    # 2) 三线表 / 无框线表：caption 锚定 + 词坐标对齐重建
    for caption in _find_caption_rects(page, fitz):
        rebuilt = _rebuild_borderless_table(page, fitz, caption)
        if not rebuilt:
            continue
        rect, md = rebuilt
        if any(_bbox_iou(rect, s) > 0.5 for s in seen):
            continue
        if _is_credible_table(md, rect, page.rect):
            found.append((rect, md))
            seen.append(rect)
    return found


def _block_text(block: dict) -> str:
    """从 get_text('dict') 的文本块中拼接所有 span 文本。"""
    parts = []
    for line in block.get("lines", []):
        parts.append("".join(span.get("text", "") for span in line.get("spans", [])))
    return "\n".join(parts)


def _is_header_footer(text: str, rect, page_height: float, zone: float = 52.0) -> bool:
    """判断靠近页面上下边缘的短文本是否为页眉 / 页脚 / 孤立页码。"""
    compact = " ".join(text.split()).strip()
    near_edge = rect.y0 < zone or rect.y1 > page_height - zone
    if not near_edge or not compact:
        return False
    if len(compact) <= 8 and re.fullmatch(r"[\d\-\s/]+", compact):
        return True
    if re.match(r"(?i)^(page|第)\s*\d+", compact):
        return True
    if "©" in compact or "copyright" in compact.lower():
        return True
    return False


def _save_image_block(block: dict, doc, assets_dir: Path, assets_name: str,
                      paper_id: str, page_no: int, index: int) -> str | None:
    """导出 get_text('dict') 的图片块，返回 Markdown 可用的相对引用。

    借鉴 pdf2md-agent-skill：跳过小图标与覆盖整页的扫描底图（后者应由 OCR 处理）。
    """
    try:
        raw = block.get("image")
        ext = (block.get("ext") or "png").lower()
        if not raw and block.get("xref"):
            info = doc.extract_image(block["xref"])
            raw, ext = info.get("image"), (info.get("ext") or "png").lower()
        if not raw:
            return None
        width = block.get("width", 0) or 0
        height = block.get("height", 0) or 0
        if width and height and (width < 60 or height < 60):
            return None
        if ext not in IMAGE_EXTS:
            ext = "png"
        fname = f"{paper_id}_p{page_no}_{index}.{ext}"
        (assets_dir / fname).write_bytes(raw)
        return f"{assets_name}/{fname}"
    except Exception:  # noqa: BLE001
        return None


def _convert_with_pymupdf_enhanced(
    pdf_path: Path, out_path: Path, assets_dir: Path,
    paper_id: str, table_strategy: str,
) -> str:
    """方案 C：不依赖 pymupdf4llm 的增强实现。

    按 bbox 统一排序「文本块 / 图片块 / 表格」，落在表格区域内的文本块跳过，
    表格在原位置插入（借鉴 layout_extractor.py），图片导出为 Markdown 引用。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise RuntimeError("缺少 PyMuPDF，请先运行 pip install pymupdf") from exc

    assets_dir.mkdir(parents=True, exist_ok=True)
    assets_name = assets_dir.name

    doc = fitz.open(str(pdf_path))
    parts: list[str] = []
    for page in doc:
        page_no = page.number + 1
        tables = _detect_tables(page, fitz, table_strategy)
        table_rects = [rect for rect, _ in tables]

        blocks = page.get_text("dict")["blocks"]
        elements: list[tuple[float, float, str]] = []

        for block in blocks:
            if not isinstance(block, dict) or "bbox" not in block:
                continue
            rect = fitz.Rect(block["bbox"])

            # 图片块永远导出：即使它落在表格区域内（表格里的示意图）也不丢弃。
            # 之前把图片纳入表格 bbox 过滤，导致三线表页面所有图片消失。
            if block.get("type") == 1:
                ref = _save_image_block(block, doc, assets_dir, assets_name, paper_id, page_no, len(elements))
                if ref:
                    elements.append((rect.y0, rect.x0, f"![Figure p{page_no}]({ref})"))
                continue

            # 只有文本块需要按表格区域去重
            if any(_cover_ratio(rect, tr) > 0.55 for tr in table_rects):
                continue
            text = _block_text(block)
            if not text.strip() or _is_header_footer(text, rect, page.rect.height):
                continue
            formatted = _format_block(text)
            if formatted:
                elements.append((rect.y0, rect.x0, formatted))

        # 兜底：某些 PDF 的图片不出现在 get_text("dict") 中，改用 image_info
        if not any(isinstance(b, dict) and b.get("type") == 1 for b in blocks):
            for idx, info in enumerate(page.get_image_info(xrefs=True)):
                xref = info.get("xref")
                if not xref:
                    continue
                try:
                    extracted = doc.extract_image(xref)
                    ext = (extracted.get("ext") or "png").lower()
                    if ext not in IMAGE_EXTS:
                        ext = "png"
                    fname = f"{paper_id}_p{page_no}_x{idx}.{ext}"
                    (assets_dir / fname).write_bytes(extracted["image"])
                    y0 = info.get("bbox", (0, 0, 0, 0))[1]
                    elements.append((y0, 0.0, f"![Figure p{page_no}]({assets_name}/{fname})"))
                except Exception:  # noqa: BLE001
                    continue

        for rect, md in tables:
            elements.append((rect.y0, rect.x0, md))

        # 按 y 分行、同行按 x 排序，尽量还原双栏阅读顺序
        elements.sort(key=lambda e: (round(e[0] / 3), e[1]))
        content = "\n\n".join(item for _, _, item in elements if item.strip())
        content = strip_page_noise(content)
        parts.append(f"## Page {page_no}\n\n{content}")

    doc.close()
    markdown = "\n\n".join(parts)
    out_path.write_text(markdown, encoding="utf-8")
    return markdown


# ------------------------------ 公共入口 ------------------------------

def parse_pdf_to_markdown(
    pdf_path: str | Path,
    language: str = "auto",
    paper_id: str | None = None,
    prefer_external_skill: bool = True,
    save_to_library: bool = True,
    backend: str | None = None,
    write_images: bool | None = None,
    table_strategy: str | None = None,
    dpi: int | None = None,
) -> str:
    """把 PDF / 文本文件转换为 Markdown，并存入论文库。

    Args:
        pdf_path: 源 PDF（或 .md/.txt）路径。
        language: 'auto' / 'zh' / 'en'，auto 读取前两页自动判断。
        paper_id: 文献唯一标识；默认取文件名。
        prefer_external_skill: 是否允许优先使用 pdf2md-agent-skill（方案 A）。
        save_to_library: 是否把结果写入 paper_library/<paper_id>.md。
        backend: auto / pdf2md_skill / pymupdf4llm / pymupdf_enhanced，默认取配置 PDF_BACKEND。
        write_images: 是否导出图片并在 Markdown 中引用，默认取配置 PDF_WRITE_IMAGES。
        table_strategy: lines / lines_strict / text / explicit，默认取配置 PDF_TABLE_STRATEGY。
                       学术论文三线表建议用 'text'。
        dpi: 图片导出分辨率，默认取配置 PDF_IMAGE_DPI。

    Returns:
        Markdown 全文字符串。图片写入 <library>/<paper_id>_assets/ 并以相对路径引用。

    Raises:
        FileNotFoundError: 源文件不存在。
        RuntimeError: 所有可用后端都失败。
    """
    s = get_settings()
    path = Path(pdf_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")

    pid = sanitize_paper_id(paper_id or path.stem)
    library_dir = s.paper_library_dir.expanduser().resolve()
    out_path = library_dir / f"{pid}.md"
    assets_dir = assets_dir_for(library_dir, pid)
    if save_to_library:
        library_dir.mkdir(parents=True, exist_ok=True)

    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        markdown = path.read_text(encoding="utf-8", errors="replace")
        if save_to_library:
            out_path.write_text(markdown, encoding="utf-8")
        return markdown

    chosen = (backend or s.pdf_backend or "auto").lower()
    if chosen not in BACKENDS:
        raise ValueError(f"未知解析后端 {chosen}，可选：{', '.join(sorted(BACKENDS))}")
    strategy = (table_strategy or s.pdf_table_strategy or "lines_strict").lower()
    if strategy not in TABLE_STRATEGIES:
        raise ValueError(f"未知表格策略 {strategy}，可选：{', '.join(sorted(TABLE_STRATEGIES))}")
    want_images = s.pdf_write_images if write_images is None else write_images
    image_dpi = dpi or s.pdf_image_dpi
    lang = detect_language(path) if language == "auto" else language

    failures: list[str] = []

    if prefer_external_skill and chosen in ("auto", "pdf2md_skill") and suffix in PDF_SUFFIXES and s.pdf2md_available:
        try:
            return _convert_with_pdf2md_skill(path, out_path, lang)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"方案 A（pdf2md-agent-skill）：{exc}")

    if chosen in ("auto", "pymupdf4llm") and suffix in PDF_SUFFIXES:
        try:
            return _convert_with_pymupdf4llm(path, out_path, assets_dir, strategy, want_images, image_dpi)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"方案 B（pymupdf4llm）：{exc}")

    if chosen in ("auto", "pymupdf_enhanced") and suffix in PDF_SUFFIXES:
        try:
            return _convert_with_pymupdf_enhanced(path, out_path, assets_dir, pid, strategy)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"方案 C（PyMuPDF 增强）：{exc}")

    if not failures:
        failures.append(f"暂不支持的格式：{suffix}（方案 A 可支持 DOCX/PPTX/XLSX 等）")
    raise RuntimeError("文献解析失败，已尝试的路径：\n- " + "\n- ".join(failures))


# ------------------------------ 论文库管理 ------------------------------

def list_paper_library() -> list[dict]:
    """列出论文库中已入库的文献（供 Streamlit 论文库 Tab 使用）。"""
    s = get_settings()
    folder = s.paper_library_dir.expanduser().resolve()
    if not folder.exists():
        return []
    items = []
    for f in sorted(folder.glob("*.md")):
        stat = f.stat()
        assets = assets_dir_for(folder, f.stem)
        image_count = len([p for p in assets.glob("*") if p.suffix.lower().lstrip(".") in IMAGE_EXTS]) if assets.exists() else 0
        items.append(
            {
                "paper_id": f.stem,
                "path": str(f),
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": stat.st_mtime,
                "images": image_count,
                "assets_dir": str(assets) if assets.exists() else "",
            }
        )
    return items


def list_paper_images(paper_id: str) -> list[str]:
    """列出某篇文献导出的图片文件路径（供界面预览 / 人工校对）。"""
    s = get_settings()
    folder = assets_dir_for(s.paper_library_dir.expanduser().resolve(), sanitize_paper_id(paper_id))
    if not folder.exists():
        return []
    return [str(p) for p in sorted(folder.iterdir()) if p.suffix.lower().lstrip(".") in IMAGE_EXTS]


def load_paper(paper_id: str) -> str:
    """按 paper_id 读取论文库中的 Markdown。"""
    s = get_settings()
    path = s.paper_library_dir.expanduser().resolve() / f"{sanitize_paper_id(paper_id)}.md"
    if not path.exists():
        raise FileNotFoundError(f"论文库中不存在：{path}")
    return path.read_text(encoding="utf-8", errors="replace")


def save_paper(paper_id: str, markdown_text: str) -> str:
    """保存（含人工校对后的）Markdown 到论文库，返回文件路径。"""
    s = get_settings()
    folder = s.paper_library_dir.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sanitize_paper_id(paper_id)}.md"
    path.write_text(markdown_text, encoding="utf-8")
    return str(path)
