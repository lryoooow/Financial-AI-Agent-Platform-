"""报告四段并行·并发 2（v1.7.0）的实际验证逻辑（被 test_report_parallel.py 以子进程调用）。

单独成文件且下划线开头：不被 pytest 直接收集，只在自己的进程里跑——
直接改 report 模块级信号量/超时常量，不污染共享 pytest 会话。

覆盖：
1. 四段并行全 llm，section_start 齐发前置，report_section 每段一条
2. 并发闸真实限流（在飞峰值 ≤2 且确实 ≥2 并行）
3. 排队等闸不消耗单段超时预算（gate/timeout 嵌套顺序的核心回归）
4. 单段异常仅该段回退模板（gather 哨兵 + zip 兜底）

分流机制：_llm_section 的 system prompt 由 section_system_prompt(key, template) 生成，
测试把它替换为 "[key:title]" 标记，fake LLM 据此路由到不同行为——不改生产代码。
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + tempfile.mkdtemp() + "/t.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from finresearch.agents.workers import report as rep  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


_QUOTE = {
    "name": "贵州茅台", "industry": "白酒",
    "price": 1523.0, "changePct": 1.24, "pe": 22.4, "pb": 8.1,
    "marketCap": "1.9万亿", "as_of": "2026-09-01 15:00",
    "provenance": {"source": "eastmemory-direct".replace("memory", "money")},
}
_FINS = {
    "years": ["2024", "2025"],
    "rows": [
        {"name": "营业收入", "values": ["100.0亿", "120.0亿"]},
        {"name": "净利润", "values": ["10.0亿", "9.0亿"]},
        {"name": "ROE", "values": ["15.0%", "14.0%"]},
    ],
}
# kline 仅用于 _has_indicators 判定（非空即可），事实构建不会展开它
_FV = {"quote": _QUOTE, "fins": _FINS, "profile": {},
       "kline": [{"close": 1.0, "date": "2026-09-01"}], "news": []}

_peak = {"now": 0, "max": 0}


class _FakeLLM:
    """astream：只计数在飞峰值，**不得**再获取生产闸——_llm_section 已在生产侧
    `async with _LLM_GATE`（timeout 外），fake 再 acquire 同一把信号量会自死锁
    （Semaphore 不可重入：一段独占两槽，其余段饿死——首版测试的真实教训）。
    hold：进入后先睡 N 秒再吐字（持有生产闸的时长，供排队用例构造）；fail：立即抛错。"""

    def __init__(self, tokens, *, delay=0.0, hold=0.0, fail=False):
        self._tokens = tokens
        self._delay = delay
        self._hold = hold
        self._fail = fail

    async def astream(self, _messages):
        if self._fail:
            raise RuntimeError("boom")
        _peak["now"] += 1
        _peak["max"] = max(_peak["max"], _peak["now"])
        try:
            if self._hold:
                await asyncio.sleep(self._hold)
            for t in self._tokens:
                await asyncio.sleep(self._delay)
                yield type("Chunk", (), {"content": t})()
        finally:
            _peak["now"] -= 1


class _Router:
    """按 system prompt 里的 [key:title] 标记把不同段路由到不同 fake。

    注意必须是普通方法（返回异步生成器）——async def 会把调用结果变成
    coroutine，_llm_section 的 `async for llm.astream(...)` 直接 TypeError。
    """

    def __init__(self, default: _FakeLLM, overrides: dict):
        self._default = default
        self._overrides = overrides

    def astream(self, messages):
        text = str(messages)
        for key, fake in self._overrides.items():
            if f"[{key}:" in text:
                return fake.astream(messages)
        return self._default.astream(messages)


def _setup(events: list):
    rep._stream_push = lambda p: events.append((p.get("type"), p.get("title"))) \
        if isinstance(p, dict) else None
    title_by_key = {s.key: s.title for s in rep._section_specs("equity_deep")}
    rep.section_system_prompt = lambda key, template: f"[{key}:{title_by_key[key]}]"
    _peak["now"] = _peak["max"] = 0
    return title_by_key


async def t_parallel_all_llm_and_event_order():
    events: list = []
    title_by_key = _setup(events)

    def fake_build_llm(**kwargs):
        return _FakeLLM(["事实", "分析", "。"], delay=0.02)

    rep.build_llm = fake_build_llm
    state = {"template": "equity_deep", "normalized_ticker": "600519.SH", "facts": dict(_FV),
             "llm_config": {"base_url": "https://x", "api_key": "k", "model": "deepseek-chat"}}
    out = await rep.report_node(state)
    sections = {s["title"]: s for s in out["draft_report"]["sections"]}
    # 只判 4 个 LLM 段（"证据分层与数据来源"是规则生成，本就不含 LLM 文本）
    ok_bodies = {s.title: "事实分析。" in sections[s.title]["body"] for s in rep._section_specs("equity_deep")}
    check(all(ok_bodies.values()), f"四段全部走 LLM 路径（实际 {ok_bodies}）")
    starts = [e for e in events if e[0] == "section_start"]
    finished = [e for e in events if e[0] == "report_section"]
    check(len(starts) == 4, f"section_start 每段一条（实际 {len(starts)}）")
    check(len(finished) == 4, f"report_section 每段一条（实际 {len(finished)}）")
    # 占位前置：最后一个 section_start 的位置先于第一个 report_section
    idx_last_start = max(events.index(e) for e in starts)
    idx_first_finish = min(events.index(e) for e in finished)
    check(idx_last_start < idx_first_finish, "section_start 齐发先于任何段落收口")
    check(_peak["max"] >= 2, f"确实并行（在飞峰值 {_peak['max']}）")
    check(_peak["max"] <= 2, f"闸限流生效：在飞峰值 ≤2（实际 {_peak['max']}）")


async def t_queue_wait_does_not_eat_budget():
    """核心回归：gate=1，首段持闸 0.6s（自身预算内存活），其余三段各需流式 0.5s。

    新嵌套（gate 外 / timeout 在获闸后才启动）：排队 0.6s 不计入预算，四段全过；
    旧嵌套（timeout 包 gate）：排队段只剩 1.0-0.6=0.4s < 0.5s，三段必回退模板。"""
    old_gate, old_timeout = rep._LLM_GATE, rep._SECTION_TIMEOUT
    rep._LLM_GATE = asyncio.Semaphore(1)
    rep._SECTION_TIMEOUT = 1.0
    events: list = []
    title_by_key = _setup(events)

    def fake_build_llm(**kwargs):
        return _Router(
            _FakeLLM(["快", "速"], delay=0.25),          # 2 token × 0.25s = 0.5s 流式
            overrides={"technical": _FakeLLM(["慢", "段"], hold=0.6)},  # 持闸 0.6s，瞬时吐字
        )

    rep.build_llm = fake_build_llm
    try:
        state = {"template": "equity_deep", "normalized_ticker": "600519.SH", "facts": dict(_FV)}
        out = await rep.report_node(state)
        sections = {s["title"]: s for s in out["draft_report"]["sections"]}
        slow_ok = "慢段" in sections[title_by_key["technical"]]["body"]
        others_ok = all(
            "快速" in sections[s.title]["body"]
            for s in rep._section_specs("equity_deep") if s.key != "technical"
        )
        check(slow_ok and others_ok,
              f"排队不耗预算：四段全过（慢段={slow_ok}，其余={others_ok}；旧嵌套下其余三段必回退模板）")
    finally:
        rep._LLM_GATE = old_gate
        rep._SECTION_TIMEOUT = old_timeout


async def t_single_section_failure_only_that_section_falls_back():
    events: list = []
    title_by_key = _setup(events)

    def fake_build_llm(**kwargs):
        return _Router(
            _FakeLLM(["正", "常"]),
            overrides={"valuation": _FakeLLM([], fail=True)},
        )

    rep.build_llm = fake_build_llm
    state = {"template": "equity_deep", "normalized_ticker": "600519.SH", "facts": dict(_FV),
             "llm_config": {"base_url": "https://x", "api_key": "k", "model": "deepseek-chat"}}
    out = await rep.report_node(state)
    sections = {s["title"]: s for s in out["draft_report"]["sections"]}
    bad = sections[title_by_key["valuation"]]["body"]
    good = [sections[s.title]["body"] for s in rep._section_specs("equity_deep") if s.key != "valuation"]
    check("正常" not in bad and bad.strip(), f"异常段回退模板非空（实际 {bad[:30]!r}）")
    check(all("正常" in b for b in good), "其余段不受异常段影响（并行异常隔离）")


async def main() -> None:
    await t_parallel_all_llm_and_event_order()
    await t_queue_wait_does_not_eat_budget()
    await t_single_section_failure_only_that_section_falls_back()
    if fails:
        print(f"\n{len(fails)} FAILURES")
        sys.exit(1)
    print("ALL REPORT-PARALLEL TESTS PASS")


if __name__ == "__main__":
    asyncio.run(main())
