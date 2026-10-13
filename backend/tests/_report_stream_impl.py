"""报告分段真流式（方案A）的实际验证逻辑（被 test_report_stream.py 以子进程调用）。

用假的流式 LLM + 捕获 _stream_push，验证：token 逐个经 section_token 推出、
拼接即最终正文；空返回与禁用词命中时回退规则模板且不把脏内容流给前端。
以子进程跑、下划线开头：不被 pytest 直接收集，避免污染共享会话。
"""
import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import finresearch.agents.workers.report as report  # noqa: E402
from finresearch.agents.workers.report import SectionSpec, _llm_section  # noqa: E402

captured: list = []
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeChunk:
    def __init__(self, content):
        self.content = content


class FakeStreamLLM:
    def __init__(self, tokens):
        self._tokens = tokens

    async def astream(self, messages):
        for t in self._tokens:
            await asyncio.sleep(0)
            yield FakeChunk(t)


async def run_case(tokens, label):
    captured.clear()
    report._stream_push = lambda msg: captured.append(msg)
    report.build_llm = lambda **kw: FakeStreamLLM(tokens)
    spec = SectionSpec(
        key="fundamentals", title="测试板块",
        has_data=lambda fv: True,
        build_facts=lambda fv: "【事实】营收 100 亿元。",
        fallback=lambda fv: "这是非空的回退模板内容。",
        max_chars=320,
    )
    state = {"template": "equity_deep", "task_id": "t1", "profile": None, "llm_config": None}
    body, source = await _llm_section(state, spec, {})
    toks = [m for m in captured if isinstance(m, dict) and m.get("type") == "section_token"]
    print(f"[{label}] source={source} body_len={len(body)} tokens_pushed={len(toks)}")
    return body, source, toks


async def main():
    body, source, toks = await run_case(["公司", "基本面", "稳健", "，", "营收", "增长", "。"], "normal")
    check(source == "llm", f"normal → source=llm (got {source})")
    check(len(toks) >= 5, f"normal → 逐 token 推送 (got {len(toks)})")
    check("".join(t["text"] for t in toks) == body, "normal → 推送 token 拼接 == 最终 body")
    check(all("text" in t and "title" in t for t in toks), "normal → 每个 section_token 带 text+title")

    _, source2, toks2 = await run_case(["", "   ", ""], "empty")
    check(source2 == "template", f"empty → 回退模板 (got {source2})")
    check(len(toks2) == 0, f"empty → 不推空/前导空白 token (got {len(toks2)})")

    fw = next(iter(report.FORBIDDEN_VERBS))
    body3, source3, toks3 = await run_case(["投资", fw, "该股"], "forbidden")
    check(source3 == "llm", f"forbidden → 裸词收口替换不落模板 (got {source3})")
    check(len(toks3) > 0, f"forbidden → 流不中断 (got {len(toks3)} tokens)")
    check(fw not in body3, f"forbidden → 收口后正文无裸词 (body={body3!r})")

    print("\n" + ("ALL STREAMING TESTS PASS" if not fails else f"FAILURES: {fails}"))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    asyncio.run(main())
