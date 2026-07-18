"""报告渲染（Jinja2）。

铁律：渲染前用 pydantic 校验 as_of / sources / risks / disclaimer 四字段非空，
缺则抛错（绝不让前端拿到残缺报告）。
PDF 可选：weasyprint 未安装则 try/except 跳过。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "report_templates"

DISCLAIMER = (
    "本报告由 AI 辅助生成，所有数值由本地工具库（akshare / pandas-ta-classic）"
    "计算并可溯源，仅供研究参考，不构成任何投资建议。市场有风险，决策需谨慎。"
)


class Source(BaseModel):
    title: str
    url: str
    date: str


class FinancialRow(BaseModel):
    name: str
    values: List[str]


class Financials(BaseModel):
    years: List[str]
    rows: List[FinancialRow]


class Section(BaseModel):
    title: str
    body: str


class ReportDoc(BaseModel):
    """与前端 ReportDoc 对齐，并加严格校验。"""

    ticker: str
    name: Optional[str] = ""
    template: str = "equity_deep"
    summary: str
    catalyst: str = ""
    financials: Financials
    sections: List[Section]
    risks: List[str] = Field(min_length=1)
    sources: List[Source] = Field(min_length=1)
    disclaimer: str
    as_of: str
    quote: Optional[dict] = None  # 1.2.3：行情速览卡片数据

    @field_validator("as_of", "disclaimer")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("as_of / disclaimer 不能为空")
        return v

    @field_validator("risks", "sources")
    @classmethod
    def _list_non_empty(cls, v):
        if not v:
            raise ValueError("risks / sources 不能为空")
        return v


_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def validate(doc: dict) -> ReportDoc:
    """校验并规范化。缺关键字段抛 ValueError。"""
    try:
        return ReportDoc(**doc)
    except Exception as e:  # noqa: BLE001
        logger.error("报告校验失败：%s", e)
        raise ValueError(f"报告校验失败：{e}") from e


def render_html(doc: dict) -> str:
    """渲染 HTML（先校验）。template 不存在则回退 base.html。"""
    m = validate(doc)
    tpl_name = f"{m.template}.html"
    try:
        _env.get_template(tpl_name)
        tpl = _env.get_template(tpl_name)
    except Exception:  # noqa: BLE001
        tpl = _env.get_template("base.html")
    return tpl.render(doc=m.model_dump(), disclaimer=DISCLAIMER)


def render_pdf(html: str) -> Optional[bytes]:
    """可选 PDF 导出。weasyprint 未安装返回 None。"""
    try:
        from weasyprint import HTML  # type: ignore

        return HTML(string=html).write_pdf()
    except Exception as e:  # noqa: BLE001
        logger.info("PDF 导出跳过（weasyprint 未安装或失败）：%s", e)
        return None


def build_doc(
    *,
    ticker: str,
    name: str,
    template: str,
    summary: str,
    catalyst: str,
    financials: dict,
    sections: list,
    risks: list,
    sources: list,
    as_of: Optional[str] = None,
    quote: Optional[dict] = None,
) -> dict:
    """便捷构造函数（自动补 disclaimer / as_of / quote）。"""
    return {
        "ticker": ticker,
        "name": name,
        "template": template,
        "summary": summary,
        "catalyst": catalyst,
        "financials": financials,
        "sections": sections,
        "risks": risks or ["（无显式风险项）"],
        "sources": sources or [{"title": "—", "url": "", "date": ""}],
        "disclaimer": DISCLAIMER,
        "as_of": as_of or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "quote": quote or {},
    }
