"""报告真删除的 pytest 包装：在独立子进程里跑 _report_delete_impl，
用临时 DB，隔离共享会话的 DB 引擎单例，避免污染其它用例。"""
import subprocess
import sys
from pathlib import Path


def test_report_true_delete():
    impl = Path(__file__).parent / "_report_delete_impl.py"
    r = subprocess.run(
        [sys.executable, str(impl)],
        capture_output=True, text=True, timeout=180,
    )
    assert "ALL DELETE TESTS PASS" in r.stdout, (
        f"returncode={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    assert r.returncode == 0
