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
