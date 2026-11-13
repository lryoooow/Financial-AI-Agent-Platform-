"""报告真实文件导出：txt / md / docx / pdf。

统一以 ReportDoc 形状的 dict（Report.payload）为输入，渲染**完整结构化内容**
（摘要 + 财务表 + 章节 + 风险 + 来源 + 免责），不只导出摘要。
- docx：python-docx（封面信息 + 摘要 + 财务表 + 章节 + 风险列表 + 来源 + 免责）。
- pdf：reportlab Platypus（注册 STSong-Light CID 字体保证中文正常渲染，不依赖 weasyprint/GTK）。
- txt / md：同一份结构化报告纯文本/Markdown。
模型 API Key 绝不写入导出文件。
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
from pathlib import Path
from typing import Any

_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# XML 1.0 非法控制字符（python-docx / lxml 与 reportlab XML parser 遇到必抛 ValueError）
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def _clean_xml_str(val: Any) -> str:
    if val is None:
        return ""
    return _XML_ILLEGAL_RE.sub("", str(val))


def _normalize(doc: dict) -> dict:
    fins = doc.get("financials") or {}
    raw_rows = list(fins.get("rows", []) or [])
    clean_rows = [
        {
            "name": _clean_xml_str(r.get("name", "")),
            "values": [_clean_xml_str(v) for v in (r.get("values") or [])],
        }
        for r in raw_rows
        if isinstance(r, dict)
    ]
    raw_sections = list(doc.get("sections", []) or [])
    clean_sections = [
        {
            "title": _clean_xml_str(s.get("title", "")),
            "body": _clean_xml_str(s.get("body", "")),
        }
        for s in raw_sections
        if isinstance(s, dict)
    ]
    raw_sources = list(doc.get("sources", []) or [])
    clean_sources = [
        {
            "title": _clean_xml_str(s.get("title", "")),
            "date": _clean_xml_str(s.get("date", "")),
            "url": _clean_xml_str(s.get("url", "")),
        }
        for s in raw_sources
        if isinstance(s, dict)
    ]
    return {
        "ticker": _clean_xml_str(doc.get("ticker", "")),
        "name": _clean_xml_str(doc.get("name", "")),
        "template": _clean_xml_str(doc.get("template", "equity_deep") or "equity_deep"),
        "summary": _clean_xml_str(doc.get("summary", "")),
        "catalyst": _clean_xml_str(doc.get("catalyst", "")),
        "years": [_clean_xml_str(y) for y in (fins.get("years", []) or [])],
        "rows": clean_rows,
        "sections": clean_sections,
        "risks": [_clean_xml_str(r) for r in (doc.get("risks", []) or [])],
        "sources": clean_sources,
        "disclaimer": _clean_xml_str(doc.get("disclaimer", "")),
        "as_of": _clean_xml_str(doc.get("as_of", "")),
    }


def safe_filename(doc: dict, ext: str) -> str:
    d = _normalize(doc)
    base = (d["name"] or d["ticker"] or "JingYuan-Report").strip() or "report"
    # 保留 A 股退市风险警示前缀 *ST 的可读性（全角 ＊ST 在 Windows/macOS 文件名合法）
    base = re.sub(r"\*ST", "＊ST", base, flags=re.IGNORECASE)
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    for ch in '\\/:*?"<>|':
        base = base.replace(ch, "_")
    base = base.strip(" .")[:100] or "report"
    if base.upper().split(".")[0] in _WIN_RESERVED_NAMES:
        base = f"_{base}"
    clean_ext = re.sub(r"[^A-Za-z0-9]", "", ext or "pdf") or "pdf"
    return f"{base}.{clean_ext}"


# ────────────────────────────────────────────────────────────
# TXT
# ────────────────────────────────────────────────────────────
def render_txt(doc: dict) -> str:
    d = _normalize(doc)
    L: list[str] = [
        f"{d['name'] or d['ticker']} 投资研究报告",
        "=" * 40,
        f"标的：{d['ticker']}    报告日期：{d['as_of'] or dt.date.today().isoformat()}",
        "",
        "【投资摘要】",
        d["summary"],
        "",
    ]
    if d["catalyst"]:
        L += ["【催化剂】", d["catalyst"], ""]
    if d["rows"]:
        L.append("【财务概览】")
        L.append("指标".ljust(12) + "".join(str(y).rjust(14) for y in d["years"]))
        for r in d["rows"]:
            L.append(str(r.get("name", "")).ljust(12) + "".join(str(v).rjust(14) for v in r.get("values", [])))
        L.append("")
    for s in d["sections"]:
        L += [f"【{s.get('title', '')}】", s.get("body", ""), ""]
    if d["risks"]:
        L.append("【风险提示】")
        L += [f"· {r}" for r in d["risks"]]
        L.append("")
    if d["sources"]:
        L.append("【引用来源】")
        for i, s in enumerate(d["sources"], 1):
            L.append(f"[{i}] {s.get('title', '')} · {s.get('date', '')} · {s.get('url', '')}")
        L.append("")
    L += ["-" * 40, d["disclaimer"]]
    return "\n".join(L)


# ────────────────────────────────────────────────────────────
# Markdown（.md 文件下载用，Markdown 在这里是合适的）
# ────────────────────────────────────────────────────────────
def _md_cell(val: Any) -> str:
    return str(val if val is not None else "").replace("|", "\\|").replace("\r", "").replace("\n", " ")


def _md_link_text(val: Any) -> str:
    return str(val if val is not None else "").replace("[", "［").replace("]", "］").replace("\r", "").replace("\n", " ")


def render_md(doc: dict) -> str:
    d = _normalize(doc)
    L: list[str] = [f"# {d['name'] or d['ticker']} 投资研究报告\n"]
    L.append(f"> 标的 **{d['ticker']}** · 报告日期 **{d['as_of']}**\n")
    L.append("## 投资摘要\n" + d["summary"] + "\n")
    if d["catalyst"]:
        L.append("## 催化剂\n" + d["catalyst"] + "\n")
    if d["rows"] and d["years"]:
        n_cols = len(d["years"])
        L.append("## 财务概览\n")
        L.append("| 指标 | " + " | ".join(_md_cell(y) for y in d["years"]) + " |")
        L.append("|" + " --- |" * (n_cols + 1))
        for r in d["rows"]:
            vals = list(r.get("values", []) or [])[:n_cols]
            if len(vals) < n_cols:
                vals.extend(["—"] * (n_cols - len(vals)))
            L.append("| " + _md_cell(r.get("name", "")) + " | " + " | ".join(_md_cell(v) for v in vals) + " |")
        L.append("")
    for s in d["sections"]:
        body_md = re.sub(r"(?m)^#{1,2}(?=\s)", "###", s.get("body", "") or "")
        L.append(f"## {s.get('title', '')}\n" + body_md + "\n")
    if d["risks"]:
        L.append("## 风险提示\n" + "\n".join(f"- {r}" for r in d["risks"]) + "\n")
    if d["sources"]:
        src_lines = []
        for i, s in enumerate(d["sources"], 1):
            title = _md_link_text(s.get("title", "") or "来源")
            url = str(s.get("url", "") or "").strip().replace(" ", "%20").replace(")", "%29")
            dt_str = str(s.get("date", "") or "").strip()
            if url.startswith(("http://", "https://")):
                src_lines.append(f"{i}. [{title}]({url})" + (f" · {dt_str}" if dt_str else ""))
            else:
                src_lines.append(f"{i}. {title}" + (f" · {dt_str}" if dt_str else ""))
        L.append("## 引用来源\n" + "\n".join(src_lines) + "\n")
    L.append("---\n" + d["disclaimer"])
    return "\n".join(L)


# ────────────────────────────────────────────────────────────
# DOCX（python-docx）
# ────────────────────────────────────────────────────────────
def render_docx(doc: dict) -> bytes:
    from docx import Document
    from docx.oxml.ns import qn

    d = _normalize(doc)
    document = Document()
    try:
        normal_style = document.styles["Normal"]
        normal_style.font.name = "Microsoft YaHei"
        normal_style._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    except Exception:  # noqa: BLE001
        pass
    document.add_heading(f"{d['name'] or d['ticker']} 投资研究报告", level=0)
    document.add_paragraph(f"标的：{d['ticker']}    报告日期：{d['as_of']}")
    document.add_heading("投资摘要", level=1)
    for para in str(d["summary"]).split("\n"):
        if para.strip():
            document.add_paragraph(para.strip())
    if d["catalyst"]:
        document.add_heading("催化剂", level=1)
        document.add_paragraph(d["catalyst"])
    if d["rows"] and d["years"]:
        document.add_heading("财务概览", level=1)
        n_cols = len(d["years"]) + 1
        table = document.add_table(rows=1, cols=n_cols)
        try:
            table.style = "Light Grid Accent 1"
        except Exception:  # noqa: BLE001
            pass
        hdr = table.rows[0].cells
        hdr[0].text = "指标"
        for i, y in enumerate(d["years"]):
            if i + 1 < len(hdr):
                hdr[i + 1].text = str(y)
        for r in d["rows"]:
            cells = table.add_row().cells
            cells[0].text = str(r.get("name", ""))
            for i, v in enumerate(r.get("values", []) or []):
                if i + 1 < len(cells):
                    cells[i + 1].text = str(v)
    for s in d["sections"]:
        document.add_heading(str(s.get("title", "")), level=1)
        for para in str(s.get("body", "")).split("\n"):
            if para.strip():
                document.add_paragraph(para)
    if d["risks"]:
        document.add_heading("风险提示", level=1)
        for r in d["risks"]:
            try:
                document.add_paragraph(str(r), style="List Bullet")
            except Exception:  # noqa: BLE001
                document.add_paragraph(f"· {r}")
    if d["sources"]:
        document.add_heading("引用来源", level=1)
        for i, s in enumerate(d["sources"], 1):
            document.add_paragraph(f"[{i}] {s.get('title', '')} · {s.get('date', '')} · {s.get('url', '')}")
    p = document.add_paragraph(d["disclaimer"])
    try:
        if p.runs:
            p.runs[0].italic = True
    except Exception:  # noqa: BLE001
        pass
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# ────────────────────────────────────────────────────────────
# PDF（reportlab + Windows TrueType 中文字体优先 / STSong-Light 兜底）
# ────────────────────────────────────────────────────────────
_CJK_FONT_NAME: str | None = None


def _ensure_cjk_font() -> str:
    global _CJK_FONT_NAME
    if _CJK_FONT_NAME:
        return _CJK_FONT_NAME
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    win_dir = Path(os.environ.get("WINDIR") or "C:/Windows") / "Fonts"
    candidates = [
        ("FinYaHei", win_dir / "msyh.ttc", 0),
        ("FinDeng", win_dir / "Deng.ttf", None),
        ("FinSimSun", win_dir / "simsun.ttc", 0),
        ("FinSimHei", win_dir / "simhei.ttf", None),
        ("FinMacHeiti", Path("/System/Library/Fonts/STHeiti Light.ttc"), 0),
        ("FinMacSongti", Path("/System/Library/Fonts/Supplemental/Songti.ttc"), 0),
        ("FinMacArialUni", Path("/Library/Fonts/Arial Unicode.ttf"), None),
        ("FinLinuxZenHei", Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"), 0),
        ("FinLinuxMicroHei", Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"), 0),
        ("FinLinuxDroidFallback", Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"), None),
    ]
    for name, font_path, sub_idx in candidates:
        if font_path.is_file():
            try:
                tt = TTFont(name, str(font_path), subfontIndex=sub_idx) if sub_idx is not None else TTFont(name, str(font_path))
                pdfmetrics.registerFont(tt)
                try:
                    pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)
                except Exception:  # noqa: BLE001
                    pass
                _CJK_FONT_NAME = name
                return name
            except Exception:  # noqa: BLE001
                continue
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _CJK_FONT_NAME = "STSong-Light"
        return "STSong-Light"
    except Exception:  # noqa: BLE001
        return "Helvetica"


def render_pdf(doc: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    d = _normalize(doc)
    buf = io.BytesIO()
    usable_w = A4[0] - 32 * mm
    pdf = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=16 * mm, rightMargin=16 * mm
    )
    cjk = _ensure_cjk_font()
    base = getSampleStyleSheet()
    st_title = ParagraphStyle("ZhTitle", parent=base["Title"], fontName=cjk, fontSize=18, leading=24)
    st_h1 = ParagraphStyle("ZhH1", parent=base["Heading1"], fontName=cjk, fontSize=13.5, leading=20, spaceBefore=8, spaceAfter=4)
    st_body = ParagraphStyle("ZhBody", parent=base["BodyText"], fontName=cjk, fontSize=10, leading=16, spaceAfter=3, wordWrap="CJK")
    st_cell = ParagraphStyle("ZhCell", parent=st_body, fontSize=8.5, leading=12, spaceAfter=0, alignment=1)
    st_cell_left = ParagraphStyle("ZhCellL", parent=st_cell, alignment=0)
    st_disc = ParagraphStyle("ZhDisc", parent=st_body, fontSize=8, leading=12, textColor=colors.grey)

    def esc(t: Any) -> str:
        return str(t if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def add_multiline(text: Any, style: ParagraphStyle) -> None:
        lines = [ln.strip() for ln in str(text or "").split("\n") if ln.strip()]
        if not lines:
            return
        for ln in lines:
            # 防止单段超长（>1500 字）超出 A4 单页高度导致 LayoutError
            for start in range(0, len(ln), 1200):
                story.append(Paragraph(esc(ln[start:start + 1200]), style))

    story: list = []
    story.append(Paragraph(esc(f"{d['name'] or d['ticker']} 投资研究报告"), st_title))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(esc(f"标的：{d['ticker']}    报告日期：{d['as_of']}"), st_body))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("投资摘要", st_h1))
    add_multiline(d["summary"], st_body)
    if d["catalyst"]:
        story.append(Paragraph("催化剂", st_h1))
        add_multiline(d["catalyst"], st_body)
    if d["rows"] and d["years"]:
        story.append(Paragraph("财务概览", st_h1))
        n_years = len(d["years"])
        first_w = min(42 * mm, usable_w * 0.28)
        col_w = [first_w] + [(usable_w - first_w) / max(1, n_years)] * n_years
        data = [[Paragraph("指标", st_cell_left)] + [Paragraph(esc(y), st_cell) for y in d["years"]]]
        for r in d["rows"]:
            vals = list(r.get("values", []) or [])[:n_years]
            if len(vals) < n_years:
                vals.extend(["—"] * (n_years - len(vals)))
            data.append([Paragraph(esc(r.get("name", "")), st_cell_left)] + [Paragraph(esc(v), st_cell) for v in vals])
        tbl = Table(data, colWidths=col_w, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), cjk),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3e8c8")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 3 * mm))
    for s in d["sections"]:
        story.append(Paragraph(esc(s.get("title", "")), st_h1))
        add_multiline(s.get("body", ""), st_body)
    if d["risks"]:
        story.append(Paragraph("风险提示", st_h1))
        for r in d["risks"]:
            add_multiline(f"· {r}", st_body)
    if d["sources"]:
        story.append(Paragraph("引用来源", st_h1))
        for i, s in enumerate(d["sources"], 1):
            parts = [p for p in (s.get("title", ""), s.get("date", ""), s.get("url", "")) if str(p or "").strip()]
            add_multiline(f"[{i}] " + " · ".join(str(p) for p in parts), st_body)
    story.append(Spacer(1, 4 * mm))
    add_multiline(d["disclaimer"], st_disc)
    pdf.build(story)
    return buf.getvalue()
