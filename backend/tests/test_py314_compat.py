"""审计决策缓解措施：Python 3.14 + pydantic v1 兼容层冒烟。

langchain_core 在 3.14 上运行时警告「Core Pydantic V1 functionality isn't compatible
with Python 3.14 or greater」。用户选择维持 3.14 打包，本文件做两件事：

1. 【实证记录】v1 在 3.14 上的确切破损形态：PEP 649（延迟注解）使 ModelMetaclass
   在类创建时拿不到 `__annotations__`，**无默认值的必填字段会被静默丢弃**
   （实测 A.__fields__ 只剩带默认值字段）。此约束固化成断言，行为一旦变化立即可知。
2. 【路径冒烟】跑通本项目实际用到的 LLM 工具路径（langchain_core @tool + invoke，
   走 pydantic v2 schema），确保 v1 的破损不会传导到工具调用。

结论：若未来升级依赖后此文件失败，优先方案是回落 Python 3.13 打包（spec 无需改动，
build_release.bat 第 2/3 步换 3.13 venv 的 python 即可）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_v1_required_fields_silently_dropped_on_314():
    """实证：3.14 + pydantic 2.12 的 v1 兼容层丢必填字段（PEP 649 延迟注解）。

    本应用的实际工具/结构化路径走 v2（见下个用例），不受影响；此断言是变更哨兵。
    """
    if sys.version_info < (3, 14):
        return  # 3.13 及以下无此问题
    from pydantic.v1 import BaseModel

    class _Spec(BaseModel):
        name: str            # 必填、无默认值
        code: str = ""       # 有默认值

    assert "code" in _Spec.__fields__
    # 已知破损形态：必填字段被丢弃。如果这条断言失败，说明 pydantic 修好了 3.14
    # 兼容（或换回了 3.13）——届时请删除本用例并回归 v1 全量路径。
    assert "name" not in _Spec.__fields__


def test_langchain_tool_with_v1_schema_invocable():
    """deepagents 工具链路冒烟：langchain_core tool 装饰 + 入参校验 + 同步调用。"""
    from langchain_core.tools import tool

    @tool
    def add(a: int, b: int) -> int:
        """加法（工具 schema 冒烟）。"""
        return a + b

    assert add.invoke({"a": 2, "b": 3}) == 5
    # 必填参数缺失时如实报错（不静默丢参）
    try:
        add.invoke({"a": 2})
        raised = False
    except Exception:
        raised = True
    assert raised, "工具必填参数缺失必须报错，不得静默通过"


def test_pydantic_v2_roundtrip_healthy():
    """主路径（v2）健康对照。"""
    from pydantic import BaseModel

    class _Spec(BaseModel):
        name: str
        limit: int = 8

    spec = _Spec(name="quote", limit=5)
    assert spec.model_dump() == {"name": "quote", "limit": 5}
    assert _Spec.model_validate(spec.model_dump()) == spec
