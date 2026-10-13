"""报告真删除的实际验证逻辑（被 test_report_delete.py 以子进程调用）。

单独成文件并以下划线开头：不被 pytest 直接收集为用例，只在自己的进程里跑，
用临时 DB，既不碰用户真实数据，也不污染共享 pytest 会话的 DB 引擎单例。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
_db = os.path.join(_tmp, "t.db").replace(os.sep, "/")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi import HTTPException  # noqa: E402
from finresearch.db.session import init_db, session_scope  # noqa: E402
from finresearch.db.models import Report, ResearchTask  # noqa: E402
from finresearch.agents.graph import (  # noqa: E402
    init_checkpointer,
    close_checkpointer,
)
from finresearch.api.routes import delete_report  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


async def seed(rid: str, tid: str, ticker: str, name: str) -> None:
    async with session_scope() as s:
        s.add(ResearchTask(id=tid, code=ticker, template="equity_deep",
                           status="done", stage="done", report_id=rid))
        s.add(Report(id=rid, task_id=tid, ticker=ticker, name=name,
                     template="equity_deep", summary="test",
                     payload={"sections": []}, html="<p>x</p>"))
        await s.commit()


async def exists(rid: str, tid: str):
    async with session_scope() as s:
        return (await s.get(Report, rid)) is not None, (await s.get(ResearchTask, tid)) is not None


async def main() -> None:
    await init_db()
    cp, kind = await init_checkpointer()
    print("checkpointer:", kind)

    # 1) 基本真删除：report + task 都物理消失；连带清检查点线程
    await seed("rep1", "task1", "600519", "贵州茅台")
    cp_ok = False
    try:
        from langgraph.checkpoint.base import empty_checkpoint
        cfg = {"configurable": {"thread_id": "task1", "checkpoint_ns": ""}}
        await cp.aput(cfg, empty_checkpoint(),
                      {"source": "test", "step": 0, "writes": {}, "parents": {}}, {})
        cp_ok = (await cp.aget(cfg)) is not None
    except Exception as e:  # noqa: BLE001
        print("  (checkpoint seed skipped:", e, ")")

    r0, t0 = await exists("rep1", "task1")
    check(r0 and t0, "seed report+task present")
    res = await delete_report("rep1")
    check(res.get("ok") is True and res.get("deleted") == "rep1", f"delete returns ok/deleted: {res}")
    r1, t1 = await exists("rep1", "task1")
    check(not r1, "report physically gone")
    check(not t1, "owning task physically gone")
    if cp_ok:
        gone = (await cp.aget({"configurable": {"thread_id": "task1", "checkpoint_ns": ""}})) is None
        check(gone, "checkpoint thread cleared")

    # 2) 再删同一个 → 404
    try:
        await delete_report("rep1")
        check(False, "re-delete raises 404")
    except HTTPException as e:
        check(e.status_code == 404, f"re-delete raises 404 (got {e.status_code})")

    # 3) 以 task_id 调用也能删（兼容路径）
    await seed("rep2", "task2", "000858", "五粮液")
    res2 = await delete_report("task2")
    check(res2.get("ok") is True and res2.get("deleted") == "rep2", f"delete-by-task_id: {res2}")
    r2, t2 = await exists("rep2", "task2")
    check(not r2 and not t2, "delete-by-task_id removed report+task")

    # 4) 进程内残留同样清掉（"真删除"闭环）：_STORE 素材 / _TASK_LLM 明文密钥 / 诊断行
    from finresearch.agents.deep import research_tools  # noqa: E402
    from finresearch.db.models import LocalSetting  # noqa: E402
    from finresearch.api import routes as routes_mod  # noqa: E402

    await seed("rep3", "task3", "300750", "宁德时代")
    research_tools._STORE["task3"] = {
        "findings": {"quote": {"price": 999.0}},
        "draft_report": {"sections": [{"title": "被删报告全文"}]},
    }
    routes_mod._TASK_LLM["task3"] = {"api_key": "sk-plain", "model": "deepseek-chat"}
    async with session_scope() as s:
        s.add(LocalSetting(key="context-diag:research:task3", payload={"items": [{"x": 1}]}))
        await s.commit()

    await delete_report("rep3")

    check("task3" not in research_tools._STORE, "删除后 _STORE 素材被清理（报告全文不滞留内存）")
    check("task3" not in routes_mod._TASK_LLM, "删除后 _TASK_LLM 明文密钥配置被清理")
    async with session_scope() as s:
        diag = await s.get(LocalSetting, "context-diag:research:task3")
    check(diag is None, "删除后 context-diag 诊断行被清理")

    await close_checkpointer()
    print("\n" + ("ALL DELETE TESTS PASS" if not fails else f"FAILURES: {fails}"))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    asyncio.run(main())
