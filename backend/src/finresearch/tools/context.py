"""上下文装配层：system prompt 公共段（画像/时间锚点/章节上下文/图表上下文）+ 诊断环形缓冲。

1.5.0 起对话历史治理已整体移交 deepagents（SummarizationMiddleware + FilesystemMiddleware），
本模块不再负责对话预算/recap/配对保护，相关常量与正则已随 legacy 链路删除。仍在服务的调用方：
- render_profile_block / build_report_section_context：研究链路各章节 system prompt 装配
- build_chart_context：图表速读上下文
- record_diag / recent_diagnostics / finalize_diag：上下文诊断环形缓冲（前端诊断页）
"""

from __future__ import annotations

import datetime as dt
import re
import time
from threading import Lock
from typing import Any, List, Optional, Tuple, Union

_DIAG: List[dict] = []
_LOCK = Lock()
_CAP = 60

# ────────────────────────────────────────────────────────────
# 用户画像（默认基线；逐字段回填，而非整体 or None）
# ────────────────────────────────────────────────────────────
DEFAULT_PROFILE = {
    "risk_preference": "稳健",
    "investment_cycle": "中期",
    "focus_metrics": "营收增速、ROE、现金流",
    "analysis_style": "稳健研究型",
}

PROFILE_LABELS = {
    "risk_preference": "风险偏好",
    "investment_cycle": "投资周期",
    "focus_metrics": "关注指标",
    "analysis_style": "分析风格",
}


def _merge_profile(profile: Optional[dict]) -> dict:
    """逐字段回填默认画像：用户只填了风险偏好，其余用默认，而非整体 or None。"""
    merged = dict(DEFAULT_PROFILE)
    if profile:
        for k in DEFAULT_PROFILE:
            v = profile.get(k)
            if v is not None and str(v).strip():
                merged[k] = str(v).strip()
    return merged


def render_profile_block(profile: Optional[dict]) -> str:
    """画像注入：中文标签 + 换行 · 前缀，消除「、」分隔符歧义。

    focus_metrics="ROE、现金流" 不会再与项间分隔符撞车（每项独占一行）。
    逐字段回填 DEFAULT_PROFILE：用户只填了风险偏好，其余走默认（产品文案承诺的
    "默认稳健研究型"）。仅当调用方显式传 None 时才完全不注入。
    """
    if profile is None:  # 显式声明"本次不注入画像"
        return ""
    merged = _merge_profile(profile)
    lines = ["【用户画像】"]
    for key, label in PROFILE_LABELS.items():
        val = merged.get(key, "")
        if val:
            lines.append(f"· {label}：{val}")
    if len(lines) == 1:
        return ""
    # 画像仅影响叙述侧重与详略，禁止据此改变、粉饰或选择性省略不利事实
    lines.append("（画像仅影响叙述侧重与详略，禁止据此改变、粉饰或选择性省略不利事实。）")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# 时间锚点（复用 TitleBar.tsx 交易时段口径：工作日 09:30–11:30 / 13:00–15:00）
# ────────────────────────────────────────────────────────────
_WEEKDAYS = "周一 周二 周三 周四 周五 周六 周日".split()


# 1.2.3：中国交易所休市日历（每年维护）
_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04", "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-05",
    "2025-06-02", "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}


def _cn_holiday(now: dt.datetime) -> bool:
    """法定节假日判断。优先 chinese_calendar（含调休数据，每年随国务院公告更新）；
    包未安装或超出覆盖年份时回退内置硬编码 _HOLIDAYS。调休补班周末返回 False（由调用方按 weekday 排除）。"""
    try:
        import chinese_calendar as cc

        # is_holiday 对普通周末也返回 True；用 get_holiday_detail 区分"法定节假日"（有具体名称）
        # 与"普通周末"（name=None），使 _trading_session 能区分"周末"与"法定节假日"两种休市
        is_hol, name = cc.get_holiday_detail(now.date())
        return bool(is_hol and name)
    except Exception:  # noqa: BLE001  NotImplementedError(超年份)/ImportError(未安装) → 回退硬编码
        return now.strftime("%Y-%m-%d") in _HOLIDAYS


def is_trading_day(now: Optional[dt.datetime] = None) -> bool:
    """判断当前日期是否为 A 股交易日（工作日且不在休市日历中）。

    供 market.py 的数据新鲜度检查使用：节假日（工作日休市）不应拒绝上一交易日数据。
    日历每年维护（与 _trading_session 共用 _HOLIDAYS）；超出日历覆盖年份时保守按工作日判断。
    """
    n = now or dt.datetime.now()
    try:
        import chinese_calendar as cc
        # is_workday 正确处理四种情况：工作日(True) / 周末(False) /
        # 法定节假日(False) / 调休补班周末(True)。旧实现 weekday()>=5 先判 False，
        # 会把调休补班的交易日误判为非交易日，导致 _ensure_fresh 画上一交易日旧线。
        return cc.is_workday(n.date())
    except Exception:  # NotImplementedError(超年份)/ImportError(未安装) → 回退硬编码（无法识别调休补班）
        if n.weekday() >= 5:
            return False
        return n.strftime("%Y-%m-%d") not in _HOLIDAYS


def _trading_session(now: Optional[dt.datetime] = None) -> str:
    n = now or dt.datetime.now()
    mins = n.hour * 60 + n.minute
    # 非交易日：由 is_trading_day 统一判断（含调休补班），再区分法定节假日 vs 周末
    if not is_trading_day(n):
        if _cn_holiday(n):
            return "A 股：休市（法定节假日）"
        return "已休市（周末）"
    # 交易日（含调休补班周末）→ 按时段细分
    if 570 <= mins <= 690:  # 09:30–11:30
        return "A 股：交易中（上午）"
    if 780 <= mins <= 900:  # 13:00–15:00
        return "A 股：交易中（下午）"
    if mins < 570:
        return "A 股：盘前"
    if 690 < mins < 780:
        return "A 股：午间休市"
    return "A 股：已收盘"


def render_time_anchor(now: Optional[dt.datetime] = None) -> str:
    """【当前时间】锚点：日期(周几) 时分 · 交易时段。

    对话链路此前完全没有时间概念，模型会拿训练截止日期推理"最近"，
    对行情工具是硬伤。
    """
    n = now or dt.datetime.now()
    wd = _WEEKDAYS[n.weekday()] if 0 <= n.weekday() < 7 else ""
    return f"【当前时间】{n.strftime('%Y-%m-%d')}（{wd}）{n.strftime('%H:%M')} · {_trading_session(n)}"


# ────────────────────────────────────────────────────────────
# 诊断环形缓冲
# ────────────────────────────────────────────────────────────
def record_diag(diag: dict) -> None:
    with _LOCK:
        _DIAG.append(diag)
        if len(_DIAG) > _CAP:
            del _DIAG[: len(_DIAG) - _CAP]


def recent_diagnostics(limit: int = 20, kind: Optional[str] = None, obj_id: Optional[str] = None) -> List[dict]:
    with _LOCK:
        items = list(_DIAG[-limit:])
    if kind:
        items = [d for d in items if d.get("kind") == kind]
    if obj_id:
        items = [d for d in items if d.get("id") == obj_id]
    return items


# ────────────────────────────────────────────────────────────
# 对话上下文装配
# ────────────────────────────────────────────────────────────
_MAX_IMAGES = 4  # 单条消息图片数量上限（多模态）


def _msg_text(msg: dict) -> str:
    """提取消息的纯文本内容，兼容 str 与多模态 list content 块。"""
    c = msg.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _img_count(msg: dict) -> int:
    """消息携带的图片数量（images 字段或多模态 content 中的 image_url 块）。"""
    imgs = msg.get("images")
    if isinstance(imgs, list):
        return len(imgs)
    c = msg.get("content")
    if isinstance(c, list):
        return sum(1 for b in c if isinstance(b, dict) and b.get("type") == "image_url")
    return 0


def _to_lc_content(msg: dict) -> Union[str, list]:
    """把存储消息转为 langchain content。

    含图 user 消息 → OpenAI 多模态 content blocks [{type:text}, {type:image_url}...]；
    其余消息（无图）→ 纯字符串（保持原有兼容，不改变格式）。
    """
    imgs = msg.get("images")
    text = _msg_text(msg)
    if imgs:
        sendable = [url for url in imgs[:_MAX_IMAGES] if isinstance(url, str) and not url.startswith("/api/context/media/")]
        archived = len(imgs[:_MAX_IMAGES]) - len(sendable)
        note = f"\n（历史消息含 {archived} 张已归档图片，本轮未自动重新发送图片字节。）" if archived else ""
        blocks: list = [{"type": "text", "text": text + note}]
        for url in sendable:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        return blocks if sendable else text + note
    return text


# ────────────────────────────────────────────────────────────
# 事实块段落边界截断（避免字符硬切出半截字段）
# ────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────
# 报告单段上下文装配
# ────────────────────────────────────────────────────────────
def estimate_tokens(s: str) -> int:
    """经验估算：中文约 1 token/1.5 字，英文/数字约 1 token/4 char。

    不引入 tiktoken：PyInstaller 联网下载 BPE 词表与离线桌面定位冲突，
    且国内模型分词器与 cl100k 不一致。这里只做粗估供预算控制，不求精确。
    """
    if not s:
        return 0
    cjk = len(re.findall(r"[一-鿿]", s))
    other = len(s) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def _truncate_facts_by_segment(facts_block: str, budget: int) -> Tuple[str, bool]:
    """按【段落边界】截断事实块：在行首「【」处切分，保留完整字段段与尾部【约束】。

    旧实现 user[:budget-3].rstrip()+"…" 是字符硬切，会切到字段值中间
    （如「资产负…」），半截值喂给 LLM 易被误读或被模型补全成编造值。
    此处改为丢弃不完整的尾段。

    无段落边界（事实块不含行首【）时回退按行截断，同样不写半截行。
    返回 (截断后文本, 是否截断)。
    """
    if not facts_block or len(facts_block) <= budget:
        return facts_block or "", False

    budget = max(1, budget)
    note = "…（事实块超出本段预算，已按完整字段段截断）"
    cap = budget - len(note)
    if cap < 1:
        return facts_block[:budget].rstrip(), True

    segs = [s.strip("\n") for s in re.split(r"(?m)(?=^【)", facts_block) if s.strip("\n")]
    if len(segs) <= 1:
        # 无段落边界：退化为按行截断，同样不留半截行
        kept: List[str] = []
        used = 0
        for ln in facts_block.splitlines():
            if used + len(ln) + 1 > cap:
                break
            kept.append(ln)
            used += len(ln) + 1
        return "\n".join(kept).rstrip() + note, True

    # 尾部【约束】优先保留：它是写作纪律，丢了比丢一段事实更危险
    body, tail = segs, ""
    if segs[-1].lstrip().startswith("【约束】"):
        tail = segs[-1]
        body = segs[:-1]

    kept = []
    used = len(tail) + 1 if tail else 0
    for seg in body:
        if used + len(seg) + 1 > cap:
            break
        kept.append(seg)
        used += len(seg) + 1

    parts = kept + ([tail] if tail else [])
    text = "\n".join(parts).rstrip()
    if not text:
        text = facts_block[:cap].rstrip()
    return text + note, True


def build_report_section_context(
    *,
    system_prompt: str,
    facts_block: str,
    profile: Optional[dict] = None,
    template: str = "equity_deep",
    section: str = "",
    obj_id: str = "",
    max_chars: Optional[int] = None,
) -> Tuple[List[Tuple[str, str]], dict]:
    """报告单段撰写上下文：system(身份/合规/段任务) → 画像 → 时间 → 事实块。

    单段事实块本身即上下文边界（估值段看不到新闻，行业段看不到 KDJ）。
    事实块超 section_facts_max_chars 时尾部截断并标注，防止单段爆上下文。
    obj_id 应为 task_id（/research/{id} 的 {id} 语义本该是任务 id）。
    """
    from ..config import settings

    t0 = time.perf_counter()
    budget = max_chars or settings.section_facts_max_chars
    sys_parts = [system_prompt.strip()]
    prof_block = render_profile_block(profile)
    if prof_block:
        sys_parts.append(prof_block)
    sys_parts.append(render_time_anchor())
    sys_final = "\n\n".join(sys_parts)

    # 事实块预算控制：超 budget 按【段落边界】截断（避免半截字段），优先保留尾部【约束】
    user, truncated = _truncate_facts_by_segment(facts_block, budget)
    lc: List[Tuple[str, str]] = [("system", sys_final), ("human", user)]
    in_chars = len(sys_final) + len(user)
    in_tokens_est = estimate_tokens(sys_final) + estimate_tokens(user)
    diag = {
        "kind": "research",
        "scenario": f"{template}:{section}",
        "id": obj_id,
        "msg_count": 2,
        "in_chars": in_chars,
        "system_chars": len(sys_final),
        "in_tokens_est": in_tokens_est,
        # budget（section_facts_max_chars）是事实块预算：pct 只反映事实块占用，与截断口径一致；
        # 系统提示长度见 system_chars（供诊断参考，不计入 pct，否则恒 >100% 而失真）
        "budget_used_pct": round(len(user) / budget * 100, 1) if budget else 0,
        "compressed": truncated,
        "facts_truncated": truncated,
        "facts_chars": len(user),
        "modules": [m for m in ["system", "profile" if prof_block else None, "time_anchor", "facts", "truncated" if truncated else None] if m],
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }
    record_diag(diag)
    return lc, diag


def build_report_context(
    *,
    system_prompt: str,
    facts_block: str,
    profile: Optional[dict] = None,
    template: str = "equity_deep",
    obj_id: str = "",
) -> Tuple[List[Tuple[str, str]], dict]:
    """整篇报告一次撰写的上下文：system(身份/合规/整篇提纲/格式/篇幅) → 画像 → 时间 → 全量事实块。

    与 build_report_section_context 的唯一差别是事实块预算：单段版按 section_facts_max_chars
    (2400) 截断，这里要装下所有段，用 report_facts_max_chars (7200)。截断逻辑复用同一个
    _truncate_facts_by_segment，按【段落边界】切，不会把某个字段拦腰截断。
    """
    from ..config import settings

    return build_report_section_context(
        system_prompt=system_prompt,
        facts_block=facts_block,
        profile=profile,
        template=template,
        section="whole",
        obj_id=obj_id,
        max_chars=settings.report_facts_max_chars,
    )


# ────────────────────────────────────────────────────────────
# 图表速读上下文装配
# ────────────────────────────────────────────────────────────
def build_chart_context(*, system_prompt: str, facts_json: str) -> List[Tuple[str, str]]:
    """图表速读：system(CHART_SYSTEM) + 事实 JSON。轻量单次调用，无历史。"""
    # 1.2.3：facts_json 防爆——超 4000 字截断（图表速读只需近期数据，无需全文 JSON）
    _CAP = 4000
    if len(facts_json) > _CAP:
        facts_json = facts_json[:_CAP].rstrip() + "\n…（JSON 已截断）"
    return [("system", system_prompt.strip()), ("human", f"【事实】(JSON)：{facts_json}\n请速读。")]


# ────────────────────────────────────────────────────────────
# 调用结果回填
# ────────────────────────────────────────────────────────────
def finalize_diag(diag: dict, duration_ms: int, *, error: str = "", note: str = "") -> dict:
    """把 LLM 调用结果回填进 build_*_context 已记录的那条 diag。

    diag 是环形缓冲里的同一个对象，就地更新即可——早期实现在装配时记一条、
    调用返回后又 record 一条，一份报告写 9 条诊断，把 60 条的缓冲冲掉大半。
    """
    diag["duration_ms"] = duration_ms
    diag["ok"] = not error
    if error:
        diag["error"] = error
    if note:
        diag["note"] = note
    return diag
