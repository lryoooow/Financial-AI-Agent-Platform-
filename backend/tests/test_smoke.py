"""冒烟测试：app 能 import + /api/health 200。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_import_app():
    from finresearch.main import app  # noqa: F401


def test_health_shape():
    # 仅校验形状定义，不发请求（CI 无 DB/akshare）
    from finresearch.api.routes import router

    paths = {r.path for r in router.routes}
    assert "/api/health" in paths
    assert "/api/quote/{code}" in paths
    assert "/api/research/{task_id}/stream" in paths


if __name__ == "__main__":
    test_import_app()
    test_health_shape()
    print("smoke ok")
