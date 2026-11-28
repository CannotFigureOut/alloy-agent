"""可复现验证：用合成 PDF 检查三线表 / 图片 / 孤立页码的解析效果。

运行（在 Skill 目录下）：
    python examples/check_parsing.py

脚本会现场生成一个学术论文风格的 PDF（booktabs 三线表 + 内嵌图片 + 页脚页码），
然后分别用 lines_strict / text 两种表格策略解析并打印结果，用于确认：
  1. 三线表被还原成 Markdown 表格，而不是压成一行纯文本；
  2. 图片被导出到 <paper_id>_assets/ 并在 Markdown 中生成相对引用；
  3. 页脚孤立页码（如 1874）被清理。
不会污染真实的 paper_library/，输出写在系统临时目录。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="mkg_parse_check_"))
os.environ["PAPER_LIBRARY_DIR"] = str(TMP / "library")

import pymupdf as fitz  # noqa: E402

import config  # noqa: E402
import pdf_parser  # noqa: E402

config.get_settings(refresh=True)


def build_synthetic_pdf(path: Path) -> None:
    """生成一页学术论文风格 PDF：三线表 + 图片 + 图注 + 页脚页码。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4

    page.insert_text((50, 60), "Table 2 Alloy composition of AA6082 and AA6086 [82,87].", fontsize=11)
    page.insert_text((50, 80), "Alloy Major element (wt.%)", fontsize=9)

    # booktabs 三线表：只有三条横线，没有竖线（这正是 find_tables 默认策略漏检的类型）
    for y in (95, 112, 172):
        page.draw_line(fitz.Point(50, y), fitz.Point(545, y), width=0.8)

    cols = [50, 115, 175, 235, 290, 345, 400, 455, 510]
    headers = ["Alloy", "Mg", "Si", "Fe", "Mn", "Cu", "Zn", "Ti", "Al"]
    for x, text in zip(cols, headers):
        page.insert_text((x, 128), text, fontsize=9)
    data_rows = [
        ["AA6082", "0.6-1.2", "0.7-1.3", "<0.5", "0.4-1", "<1", "<0.2", "<0.1", "Bal."],
        ["AA6086", "1.01", "1.58", "0.18", "0.71", "0.55", "0.17", "0.04", "Bal."],
    ]
    for idx, row in enumerate(data_rows):
        for x, text in zip(cols, row):
            page.insert_text((x, 148 + idx * 16), text, fontsize=9)

    # 内嵌图片
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 160, 110), False)
    pix.clear_with(190)
    page.insert_image(fitz.Rect(50, 220, 260, 350), stream=pix.tobytes("png"))
    page.insert_text((50, 372), "Fig. 1. Microstructure of the AA6082 alloy.", fontsize=10)

    # 页脚孤立页码
    page.insert_text((285, 815), "1874", fontsize=9)

    doc.save(str(path))
    doc.close()


def main() -> int:
    pdf_path = TMP / "synthetic_paper.pdf"
    build_synthetic_pdf(pdf_path)
    print(f"合成 PDF：{pdf_path}")
    print(f"输出目录：{TMP / 'library'}\n")

    results = {}
    for strategy in ("lines_strict", "text"):
        pid = f"check_{strategy}"
        try:
            md = pdf_parser.parse_pdf_to_markdown(
                str(pdf_path),
                paper_id=pid,
                backend="pymupdf_enhanced",
                table_strategy=strategy,
                write_images=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{strategy}] 解析失败：{exc}")
            results[strategy] = ""
            continue

        assets = pdf_parser.list_paper_images(pid)
        table_rows = pdf_parser._count_table_rows(md)
        has_1874 = "1874" in md
        results[strategy] = md
        print("=" * 70)
        print(f"策略 {strategy}：表格行数={table_rows}  导出图片={len(assets)}  含孤立页码1874={has_1874}")
        print("=" * 70)
        print(md[:1600])
        print()

    ok = True
    best = max(results.values(), key=lambda m: pdf_parser._count_table_rows(m), default="")
    if pdf_parser._count_table_rows(best) < 2:
        print("[FAIL] 未还原出 Markdown 表格")
        ok = False
    if "![" not in best:
        print("[FAIL] 未生成图片引用")
        ok = False
    if all("1874" in m for m in results.values() if m):
        print("[FAIL] 孤立页码未清理")
        ok = False
    print("\n结论：" + ("全部通过 ✅" if ok else "存在问题 ❌（见上方输出）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
