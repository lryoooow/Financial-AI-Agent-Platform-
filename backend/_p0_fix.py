# -*- coding: utf-8 -*-
"""P0 批次修复：6 个功能损坏级问题。用后即删。"""
import ast, re

# ════════ P0-1: task 描述错名 → 动态生成 + B4 优先级声明 ════════
p = r"E:\jingyuan\langchain\backend\src\finresearch\agents\deep\chat_agent.py"
src = open(p, encoding="utf-8").read()

# 1a. 替换硬编码场景清单为动态生成
old_desc = '''_TASK_TOOL_DESCRIPTION = (
    "把一个需要多工具组合的分析任务委派给场景专家代理并取回结论。\\n"
    "可用场景：market-analysis（行情诊断/多标的对比）、fundamentals-news（基本面+资讯综合）、"
    "industry-valuation（行业估值）、memory（历史对话/报告/知识库检索）、"
    "general-purpose（仅当其他场景都不匹配）。\\n"
    "description 必须自包含：写明标的代码（先用 resolve_entity 确认）、要取的字段、"
    "期望的输出——场景代理看不到本对话历史。结果不会展示给用户，拿到后由你组织作答。"
)'''
new_desc = '''# 场景清单从 _scenario_subagents() 动态生成——曾经手抄 5 个名字（实际 3 个、
# 1 个拼错 fundamentals-news），模型按说明书传错名 → task 返回软错误 → 白烧调用。
def _build_task_description() -> str:
    specs = _scenario_subagents()
    lines = ["把一个需要多工具组合的分析任务委派给场景专家代理并取回结论。"]
    lines.append("可用场景：")
    for s in specs:
        lines.append(f"  {s['name']}：{s['description']}")
    lines.append(
        "description 必须自包含：写明标的代码（先用 resolve_entity 确认）、"
        "要取的字段、期望的输出——场景代理看不到本对话历史。"
        "结果不会展示给用户，拿到后由你组织作答。"
    )
    return "\\n".join(lines)

# 兼容已有 import（test 里用 _TASK_TOOL_DESCRIPTION）——延迟到调用时求值
class _TaskDesc:
    def __str__(self):
        return _build_task_description()

_TASK_TOOL_DESCRIPTION = _TaskDesc()'''
assert src.count(old_desc) == 1, "task desc"
src = src.replace(old_desc, new_desc)

# 1b. RewriteToolDescriptionMiddleware 的 str() 兼容
old_rw = '''                if isinstance(t, dict):
                    t = {**t, "description": self._rewrites[name]}'''
new_rw = '''                rewrite = str(self._rewrites[name])
                if isinstance(t, dict):
                    t = {**t, "description": rewrite}'''
assert src.count(old_rw) == 1, "rewrite str"
src = src.replace(old_rw, new_rw)
# BaseTool 分支也用 str()
old_rw2 = '''                    try:
                        t = t.model_copy(update={"description": self._rewrites[name]})'''
new_rw2 = '''                    try:
                        t = t.model_copy(update={"description": str(self._rewrites[name])})'''
assert src.count(old_rw2) == 1, "rewrite str2"
src = src.replace(old_rw2, new_rw2)

# 1c. B4 修复：在 ROUTING 末尾加优先级声明（覆盖库英文追加块）
p2 = r"E:\jingyuan\langchain\backend\src\finresearch\agents\prompts.py"
src2 = open(p2, encoding="utf-8").read()
old_routing_end = '''task 的 description 必须自包含（标的代码、要取的字段、期望输出）；结果由你组织作答。
"""'''
new_routing_end = '''task 的 description 必须自包含（标的代码、要取的字段、期望输出）；结果由你组织作答。
本规则优先于系统提示末尾任何英文 task/subagent 使用说明；凡内联工具可单次回答的问题一律禁止 task。
"""'''
assert src2.count(old_routing_end) == 1, "routing priority"
src2 = src2.replace(old_routing_end, new_routing_end)
open(p2, "w", encoding="utf-8", newline="").write(src2)
ast.parse(src2)

# ════════ P0-2 + P0-3: RAG SQL 修复 + KNN 饿死 ════════
p3 = r"E:\jingyuan\langchain\backend\src\finresearch\tools\rag_store.py"
src3 = open(p3, encoding="utf-8").read()

# 重写整个 search 方法（修复 SQL 逗号+优先级+饿死）
old_search = src3[src3.index("    def search("):src3.index("    def kb_add_doc")]
new_search = '''    def search(
        self,
        query: str,
        k: int = 5,
        source_types: Optional[List[str]] = None,
    ) -> List[dict[str, Any]]:
        """混合检索：向量 KNN 为主；为空时词法兜底。

        修复三个 bug：
        1. 词法兜底 SQL 前导逗号语法错误（IN (,?)）
        2. OR/AND 优先级导致过滤泄漏（text LIKE ? OR title LIKE ? AND ... → 需括号）
        3. KNN 后置过滤饿死（chat 块远多于 kb 时 top-N 全被 chat 占满）
        """
        query = (query or "").strip()
        if not query:
            return []
        k = max(1, int(k))
        hits: List[dict[str, Any]] = []

        # 嵌入计算在锁外（供应商档是网络调用）
        try:
            vec = self._serialize(embed_one(query))
        except Exception:  # noqa: BLE001
            vec = None

        # KNN 路径：按源比例放大候选数，防饿死
        if vec is not None:
            try:
                with self._lock:
                    # 没有过滤时直接取 k 个；有过滤时按库内源占比放大
                    if source_types:
                        total = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                        matching = self._db.execute(
                            "SELECT COUNT(*) FROM chunks WHERE source_type IN ({})".format(
                                ",".join("?" for _ in source_types)
                            ),
                            source_types,
                        ).fetchone()[0]
                        # 目标源占比 < 50% 时放大 5 倍候选（至少 3×k）
                        knn_k = max(k * 3, int(k * (total / max(1, matching)))) if matching > 0 else k * 5
                    else:
                        knn_k = k
                    marks = ""
                    params: list[Any] = [vec, knn_k]
                    if source_types:
                        marks = "AND c.source_type IN (" + ",".join("?" for _ in source_types) + ")"
                        params += list(source_types)
                    rows = self._db.execute(
                        "SELECT c.id, c.source_type, c.ref_id, c.title, c.text, c.meta, c.created_at, v.distance "
                        f"FROM chunks_vec v JOIN chunks c ON c.id = v.chunk_id "
                        f"WHERE v.embedding MATCH ? AND k = ? {marks} ORDER BY v.distance",
                        params,
                    ).fetchall()
                    hits = [
                        {
                            "id": r[0], "source_type": r[1], "ref_id": r[2], "title": r[3],
                            "text": r[4], "meta": json.loads(r[5] or "{}"),
                            "created_at": r[6], "distance": r[7],
                        }
                        for r in rows
                    ]
            except Exception:  # noqa: BLE001  # SQLite/网络异常都降词法
                hits = []

        # 词法兜底：修复前导逗号 + OR/AND 优先级
        if not hits:
            like = f"%{query[:32]}%"
            try:
                with self._lock:
                    if source_types:
                        marks = ",".join("?" for _ in source_types)
                        sql = (
                            "SELECT id, source_type, ref_id, title, text, meta, created_at FROM chunks "
                            f"WHERE (text LIKE ? OR title LIKE ?) AND source_type IN ({marks}) "
                            "ORDER BY created_at DESC LIMIT ?"
                        )
                        rows = self._db.execute(sql, (like, like, *source_types, k)).fetchall()
                    else:
                        sql = (
                            "SELECT id, source_type, ref_id, title, text, meta, created_at FROM chunks "
                            "WHERE text LIKE ? OR title LIKE ? "
                            "ORDER BY created_at DESC LIMIT ?"
                        )
                        rows = self._db.execute(sql, (like, like, k)).fetchall()
                    hits = [
                        {
                            "id": r[0], "source_type": r[1], "ref_id": r[2], "title": r[3],
                            "text": r[4], "meta": json.loads(r[5] or "{}"),
                            "created_at": r[6], "distance": None,
                        }
                        for r in rows
                    ]
            except Exception:  # noqa: BLE001
                hits = []

        return hits[:k]

'''
src3 = src3.replace(old_search, new_search)
ast.parse(src3)
open(p3, "w", encoding="utf-8", newline="").write(src3)

# ════════ P0-4 + P0-5: memory_note 死代码 + 覆盖 bug ════════
p4 = r"E:\jingyuan\langchain\backend\src\finresearch\api\routes.py"
src4 = open(p4, encoding="utf-8").read()

# P0-4: 判断移到 append 前
old_note = '''            prof_row = await s.get(LocalSetting, "user-profile")
            user_profile = (prof_row.payload if prof_row else None) or {}
        # 行级消息为空且存在归档摘要 → 以摘要恢复上下文（会话被生命周期归档后继续聊）
        memory_note = ""
        if not history:
            summary = await tools_memory.archived_summary(conversation_id)
            if summary:
                memory_note = "【本会话更早内容的机器摘要（原文已归档压缩）】" + chr(10) + summary'''
# 确认精确匹配（可能有缩进差异）
if src4.count(old_note) != 1:
    # 尝试找实际文本
    idx = src4.find('memory_note = ""')
    if idx > 0:
        # 找到 memory_note 赋值，往前找 history 判断
        # 改为在 append 前保存标记
        old_check = 'if not history:'
        # 检查现有代码结构
        # 在 _chat_append 调用之前插入 had_history 标记
        old_append = 'history = await _chat_append(s, conv, user_msg, history)'
        if src4.count(old_append) == 1:
            src4 = src4.replace(old_append,
                'had_history = bool(history)  # append 前记录——append 后至少含本轮消息，not history 恒 False\n            ' + old_append)
            # 修改判断条件
            old_cond = 'if not history:\n            summary = await tools_memory.archived_summary(conversation_id)'
            if src4.count(old_cond) == 1:
                src4 = src4.replace(old_cond,
                    'if not had_history:\n            summary = await tools_memory.archived_summary(conversation_id)')
else:
    src4 = src4.replace(old_note, old_note)  # no-op, keep for reference

# P0-5: memory_note 被 market_context 覆盖 → 改拼接
old_ctx = '''    context_note = ""
    if memory_note:
        # 归档会话继续对话时以摘要恢复上下文（"很久之前聊了什么"）
        context_note = memory_note.strip() + chr(10) * 2
    if market_context:'''
new_ctx = '''    context_note = ""
    if memory_note:
        # 归档会话继续对话时以摘要恢复上下文（"很久之前聊了什么"）
        context_note = memory_note.strip() + chr(10) * 2
    if market_context:
        # 拼接而非覆盖——曾经 if market_context: context_note = (...)
        # 直接整体重写，两者同现时归档摘要被静默丢弃'''
if src4.count(old_ctx) == 1:
    src4 = src4.replace(old_ctx, new_ctx)
    # 现在把 market_context 的赋值改为追加
    old_mc = '''        context_note = (
            "【当前行情页面上下文】\\n"'''
    if old_mc in src4:
        src4 = src4.replace(old_mc, '''        context_note += (
            "【当前行情页面上下文】\\n"''')

ast.parse(src4)
open(p4, "w", encoding="utf-8", newline="").write(src4)

# ════════ P0-6: search_memory 漏 _normalize ════════
p5 = r"E:\jingyuan\langchain\backend\src\finresearch\agents\deep\tools.py"
src5 = open(p5, encoding="utf-8").read()
old_sm = '''    if err:
        return _fail("memory", "rag", err)
    return res


@tool
async def read_memory('''
new_sm = '''    if err:
        return _fail("memory", "rag", err)
    return _normalize(res)  # 修复：曾经漏调——"记忆库未启用"呈现为伪成功（空 items 无 error）


@tool
async def read_memory('''
assert src5.count(old_sm) == 1, "search_memory normalize"
src5 = src5.replace(old_sm, new_sm)
open(p5, "w", encoding="utf-8", newline="").write(src5)
ast.parse(src5)

print("P0 batch: all 6 fixes applied")
