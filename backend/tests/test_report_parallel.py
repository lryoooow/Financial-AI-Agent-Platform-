"""以子进程跑 _report_parallel_impl.py（改模块级常量，须隔离进程）。

env 注入 PYTHONIOENCODING=utf-8 与 test_routes_db.py 同款——impl 的 PASS/FAIL
是中文文案，子进程写侧若按本机 ANSI 代码页（cp950/cp1252）编码会在第一条
print 就 UnicodeEncodeError（评审实证），测试在这些机器上确定性假红。
"""

import os
import subprocess
import sys
from pathlib import Path


def test_report_parallel_impl():
    impl = Path(__file__).parent / "_report_parallel_impl.py"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [sys.executable, str(impl)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(Path(__file__).parent.parent),
        env=env,
    )
    assert "ALL REPORT-PARALLEL TESTS PASS" in r.stdout, (
        f"returncode={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    assert r.returncode == 0
