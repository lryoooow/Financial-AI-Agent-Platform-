"""v1.8.0 对话研究融合与上下文治理：在独立子进程里跑 _chat_v180_impl，
用临时 DB，隔离共享会话的 DB 引擎单例，避免污染其它用例。"""
import subprocess
import sys
from pathlib import Path


def test_chat_v180():
    impl = Path(__file__).parent / "_chat_v180_impl.py"
    import os

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(impl)],
        capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace", env=env,
    )
    assert "ALL CHAT-V180 TESTS PASS" in r.stdout, (
        f"returncode={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    assert r.returncode == 0
