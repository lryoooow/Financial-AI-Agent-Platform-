"""上下文装配层测试：预算、近因优先、画像注入、时间锚点、事实块边界。

对应重构里最容易悄悄坏掉的几处：recap 方向、预算把最后一轮挤掉、
画像分隔符歧义、DEFAULT_PROFILE 从未生效、事实块混进对话历史。
"""

from __future__ import annotations

import datetime as dt

import pytest

from finresearch.tools import context as ctx


def _history(n_pairs: int, body: str = "问题内容", reply: str = "【核心结论】结论内容") -> list[dict]:
    """构造 n 轮 user/assistant，末尾再补一条当前 user 提问。"""
    h: list[dict] = []
    for i in range(n_pairs):
        h.append({"role": "user", "content": f"{body}{i}" + "补" * 200})
        h.append({"role": "assistant", "content": f"{reply}{i}" + "详" * 400})
    h.append({"role": "user", "content": "这是当前这一轮的问题"})
    return h


# ────────────────────────────────────────────────────────────
# 预算
# ────────────────────────────────────────────────────────────








# ────────────────────────────────────────────────────────────
# 近因优先（旧实现从最老取，方向反了）
# ────────────────────────────────────────────────────────────
















# ────────────────────────────────────────────────────────────
# 末条非 user 防御（前端异常传入 assistant 结尾）
# ────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────
# 用户画像
# ────────────────────────────────────────────────────────────
def test_profile_uses_chinese_labels_not_raw_keys():
    block = ctx.render_profile_block({"risk_preference": "稳健"})
    assert "风险偏好：稳健" in block
    assert "risk_preference" not in block


def test_multi_value_field_does_not_collide_with_item_separator():
    """focus_metrics="ROE、现金流" 不能与项间分隔符撞车——每项独占一行。"""
    block = ctx.render_profile_block({"focus_metrics": "ROE、现金流"})
    lines = [l for l in block.splitlines() if l.startswith("· ")]
    assert len(lines) == 4  # 四个字段各一行
    assert "· 关注指标：ROE、现金流" in lines


def test_default_profile_backfills_per_field():
    block = ctx.render_profile_block({"risk_preference": "激进"})
    assert "风险偏好：激进" in block                       # 用户填的
    assert f"投资周期：{ctx.DEFAULT_PROFILE['investment_cycle']}" in block  # 默认回填


def test_empty_payload_still_yields_default_profile():
    """新装用户没有 user-profile 行 → routes 传 {} → 默认画像必须生效。"""
    block = ctx.render_profile_block({})
    assert "风险偏好：稳健" in block
    assert "分析风格：稳健研究型" in block


def test_explicit_none_disables_profile_injection():
    assert ctx.render_profile_block(None) == ""


def test_profile_block_forbids_fact_distortion():
    block = ctx.render_profile_block({})
    assert "禁止据此改变、粉饰或选择性省略不利事实" in block


# ────────────────────────────────────────────────────────────
# 时间锚点
# ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "when,expect",
    [
        (dt.datetime(2026, 7, 10, 10, 0), "交易中（上午）"),   # 周五 10:00
        (dt.datetime(2026, 7, 10, 12, 0), "午间休市"),
        (dt.datetime(2026, 7, 10, 14, 0), "交易中（下午）"),
        (dt.datetime(2026, 7, 10, 16, 0), "已收盘"),
        (dt.datetime(2026, 7, 10, 8, 0), "盘前"),
        (dt.datetime(2026, 7, 11, 10, 0), "已休市（周末）"),   # 周六
    ],
)
def test_time_anchor_reports_trading_session(when, expect):
    anchor = ctx.render_time_anchor(when)
    assert anchor.startswith("【当前时间】2026-07-1")
    assert expect in anchor




# ────────────────────────────────────────────────────────────
# 事实块边界：只进 system，绝不混入对话历史
# ────────────────────────────────────────────────────────────




# ────────────────────────────────────────────────────────────
# token 估算与诊断回填
# ────────────────────────────────────────────────────────────


def test_finalize_diag_updates_in_place_without_appending():
    before = len(ctx.recent_diagnostics(limit=100))
    _lc, diag = ctx.build_report_section_context(
        system_prompt="SYS", facts_block="F", section="technical", obj_id="task-1"
    )
    after_build = len(ctx.recent_diagnostics(limit=100))
    ctx.finalize_diag(diag, 123, error="boom")
    after_finalize = len(ctx.recent_diagnostics(limit=100))

    assert after_build == before + 1
    assert after_finalize == after_build, "finalize 不应再记一条，避免一份报告写 9 条诊断"
    assert diag["duration_ms"] == 123 and diag["ok"] is False and diag["error"] == "boom"


def test_report_section_diag_uses_task_id_as_obj_id():
    _lc, diag = ctx.build_report_section_context(
        system_prompt="SYS", facts_block="F", template="risk", section="valuation", obj_id="task-abc"
    )
    assert diag["kind"] == "research"
    assert diag["id"] == "task-abc"
    assert diag["scenario"] == "risk:valuation"


def test_oversized_facts_block_is_truncated_and_flagged():
    _lc, diag = ctx.build_report_section_context(
        system_prompt="SYS", facts_block="事" * 9000, section="technical", max_chars=1000
    )
    assert diag["facts_truncated"] is True
    assert diag["facts_chars"] <= 1000


def test_facts_block_truncation_keeps_segment_boundary():
    """多【段落】事实块超预算时按整段丢弃，不得出现半截字段标题。"""
    facts = (
        "【行情数据】\n最新价 1523.00 元\n"
        "【财务指标】\n营收 1500 亿\n净利率 46.7%\n"
        "【技术指标】\nKDJ 金叉\nMACD 红柱放大\n"
        "【约束】\n不得推算缺失值，不得编造未披露数据"
    )
    lc, diag = ctx.build_report_section_context(
        system_prompt="SYS", facts_block=facts, section="technical", max_chars=50
    )
    assert diag["facts_truncated"] is True
    user = lc[1][1]

    # 不变量 1：无半截字段——含【的行必含】（字符硬切会产出「【技术指标…」这种残行）
    for line in user.splitlines():
        if "【" in line:
            assert "】" in line, f"半截字段标题：{line!r}"

    # 不变量 2：尾段【约束】必须保留（数字纪律指令不得因截断丢失）
    assert "【约束】" in user
    assert "不得推算缺失值" in user

    # 不变量 3：整段同进同退——被丢段的正文不得残留为孤立数据行
    if "【技术指标】" not in user:
        assert "KDJ" not in user, "技术指标段标题已丢，正文 KDJ 不得残留为孤立行"
        assert "MACD" not in user
    if "【财务指标】" not in user:
        assert "净利率" not in user


# ────────────────────────────────────────────────────────────
# 文档 §3.1：recap offload 指针（offload 是 compress 的安全网）
# ────────────────────────────────────────────────────────────




# ────────────────────────────────────────────────────────────
# 文档 §4.1：按模型 profile 动态预算（按真实窗口大小映射）
# ────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────
# 文档 §4.1：LLM 摘要替代规则截取（第二级压缩 compress 核心）
# ────────────────────────────────────────────────────────────




