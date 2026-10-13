"""对话中断落库集成测试（子进程隔离临时 DB，不碰真实数据）。"""

import subprocess
import sys
from pathlib import Path

import pytest


def test_chat_interrupt_persists_partial_reply():
    """流中断/异常/并发三种路径的会话一致性（详见 _chat_interrupt_impl.py）。"""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "_chat_interrupt_impl.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"impl 失败：\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "ALL PASS" in proc.stdout
