"""报告分段真流式（方案A）的 pytest 包装：独立子进程跑 _report_stream_impl，
避免 monkeypatch report 模块全局污染共享会话。"""
import subprocess
import sys
from pathlib import Path


def test_report_section_real_streaming():
    impl = Path(__file__).parent / "_report_stream_impl.py"
    r = subprocess.run(
        [sys.executable, str(impl)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert "ALL STREAMING TESTS PASS" in (r.stdout or ""), (
        f"returncode={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    assert r.returncode == 0


def test_history_coalesces_section_tokens_for_replay():
    """回放历史段级折叠：同段连续 section_token 合并，600 帧覆盖完整长报告。

    曾经逐 chunk 一帧，3000 字报告数千帧，刷新页面后最早的段被 600 帧上限裁掉，
    "切页回来前几段没了、最后又整段蹦出来"。
    """
    import json

    from finresearch.api import routes, sse

    h: list[str] = []
    routes._coalesce_history_append(h, sse.sse({"type": "section_start", "title": "技术面分析"}))
    for tok in ("股价", "近期", "持续", "走强", "，量能", "配合"):
        routes._coalesce_history_append(h, sse.sse({"type": "section_token", "title": "技术面分析", "text": tok}))
    routes._coalesce_history_append(h, sse.sse({"type": "section_token", "title": "基本面分析", "text": "营收"}))
    routes._coalesce_history_append(h, sse.sse({"type": "tool", "text": "done"}))

    assert len(h) == 4, f"同段 token 应合并为一帧（实际 {len(h)} 帧）"
    merged = json.loads(h[1][6:].strip())
    assert merged["text"] == "股价近期持续走强，量能配合"
    other = json.loads(h[2][6:].strip())
    assert other["title"] == "基本面分析"  # 不同段的 token 不得串段合并

    # 帧数上限仍然生效
    for i in range(700):
        routes._coalesce_history_append(h, sse.sse({"type": "tool", "text": f"t{i}"}))
    assert len(h) == 600
