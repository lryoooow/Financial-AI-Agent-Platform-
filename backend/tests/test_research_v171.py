"""v1.7.1 研究链路修复（确定性编排 / 思维链透出 / 审计 A04-A06）的 pytest 包装：
独立子进程 + 临时 DB，隔离共享会话的 DB 引擎单例。"""
import os
import subprocess
import sys
from pathlib import Path


def test_research_v171():
    impl = Path(__file__).parent / "_research_v171_impl.py"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(impl)],
        capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=env,
    )
    assert "ALL RESEARCH-V171 TESTS PASS" in r.stdout, (
        f"returncode={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr[-4000:]}"
    )
    assert r.returncode == 0
