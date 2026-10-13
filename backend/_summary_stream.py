# -*- coding: utf-8 -*-
"""摘要→正文合并为一次连续 LLM 流。用后即删。"""
import ast

# ════════ 1. report.py: splitter 识别摘要段 + report_node 提取摘要 ════════
p = r"E:\jingyuan\langchain\backend\src\finresearch\agents\workers\report.py"
src = open(p, encoding="utf-8").read()

# 1a. _SectionSplitter.__init__ 加 summary_title 参数
old_init = '''    def __init__(self, titles: List[str], on_event: Callable[[dict], None]) -> None:
        self._titles = list(titles)
        self._by_norm = {self._normalize(t): t for t in titles}'''
new_init = '''    SUMMARY_TITLE = "投资摘要"  # 特殊段：token 以 summary_token 事件发出（前端打字机）

    def __init__(self, titles: List[str], on_event: Callable[[dict], None]) -> None:
        self._titles = list(titles)
        self._by_norm = {self._normalize(t): t for t in titles}'''
assert src.count(old_init) == 1, "splitter init"
src = src.replace(old_init, new_init)

# 1b. _emit_text 加摘要分支
old_emit = '''    def _emit_text(self, text: str) -> None:
        if not text or self._current is None:
            return
        self._bodies.setdefault(self._current, []).append(text)
        self._on_event({"type": "section_token", "title": self._current, "text": text})'''
new_emit = '''    def _emit_text(self, text: str) -> None:
        if not text or self._current is None:
            return
        self._bodies.setdefault(self._current, []).append(text)
        if self._current == self.SUMMARY_TITLE:
            # 摘要段走独立事件通道：前端逐字追加到摘要卡（打字机效果）
            self._on_event({"type": "summary_token", "text": text})
        else:
            self._on_event({"type": "section_token", "title": self._current, "text": text})'''
assert src.count(old_emit) == 1, "emit_text"
src = src.replace(old_emit, new_emit)

# 1c. _llm_report 的 usable 列表前插入摘要标题
old_usable = '''    usable = [s for s in specs if s.has_data(fv)]
    if not usable:
        return {}'''
new_usable = '''    usable = [s for s in specs if s.has_data(fv)]
    if not usable:
        return {}
    # 摘要并入整篇流：作为第一个分段，token 以 summary_token 逐字流出，
    # 摘要写完标题一切正文无缝续上——一条流从摘要首字到报告结尾不断
    splitter_titles = [_SectionSplitter.SUMMARY_TITLE] + [s.title for s in usable]'''
assert src.count(old_usable) == 1, "usable"
src = src.replace(old_usable, new_usable)

# 1d. _llm_report 中 splitter 构造改用合并标题列表
old_split = '''    splitter = _SectionSplitter([s.title for s in usable], _stream_push)'''
new_split = '''    splitter = _SectionSplitter(splitter_titles, _stream_push)'''
assert src.count(old_split) == 1, "splitter construct"
src = src.replace(old_split, new_split)

# 1e. _llm_report 的事实块前追加摘要事实
old_facts = '''    lc, diag = build_report_context(
        system_prompt=sys_prompt,
        facts_block=_whole_facts_block(usable, fv),'''
new_facts = '''    # 摘要事实块拼在最前（模型按提纲先写摘要再写正文）
    summary_facts = _compose_summary_block(state, fv)
    whole_facts = ("【投资摘要·事实】\\n" + summary_facts + "\\n\\n" + _whole_facts_block(usable, fv)) if summary_facts else _whole_facts_block(usable, fv)
    lc, diag = build_report_context(
        system_prompt=sys_prompt,
        facts_block=whole_facts,'''
assert src.count(old_facts) == 1, "facts"
src = src.replace(old_facts, new_facts)

# 1f. report_node: 从 whole 中提取摘要 + 发权威收口
old_extract = '''    # 整篇一次撰写：一次 LLM 调用连续写完所有部分，_SectionSplitter 边流边切段。
    # 整篇内部不再因禁用词中止（分层豁免+收口替换），异常时也保留已完成的干净段；
    # 缺失段（模型漏写/替换后仍脏/整篇异常）走单段补写——真流式、完成即收口。
    _stream_push("开始撰写报告正文…")'''
new_extract = '''    # 整篇一次撰写（摘要并入）：一次 LLM 调用从摘要首字到报告结尾连续流式。
    _stream_push("开始撰写报告…")'''
assert src.count(old_extract) == 1, "push text"
src = src.replace(old_extract, new_extract)

# 在 whole 获取后添加摘要提取
old_after = '''    whole = await _llm_report(state, specs, fv)

    results: dict[str, tuple[str, str]] = {}'''
new_after = '''    whole = await _llm_report(state, specs, fv)

    # 摘要从整篇流中提取（模型按提纲先写摘要段）
    SUMMARY_KEY = _SectionSplitter.SUMMARY_TITLE
    draft_summary = ""
    if SUMMARY_KEY in whole:
        draft_summary = whole[SUMMARY_KEY][0].strip()
        del whole[SUMMARY_KEY]  # 不作为普通 section 渲染
        # 权威收口：替换语义兜住 sanitize/strip_md 差异 + 断线重连回放
        _stream_push({"type": "research_summary", "text": draft_summary, "name": state.get("code", ""), "ticker": state.get("normalized_ticker", "")})
    else:
        draft_summary = _compose_summary_block(state, fv)
        _stream_push({"type": "research_summary", "text": draft_summary, "name": state.get("code", ""), "ticker": state.get("normalized_ticker", "")})

    results: dict[str, tuple[str, str]] = {}'''
assert src.count(old_after) == 1, "extract summary"
src = src.replace(old_after, new_after)

# 1g. report_node 底部的 build_doc summary 参数用 draft_summary
old_doc = '''    doc = report_tool.build_doc(
        ticker=_ticker,
        name=name,
        template=template,
        summary=summary,'''
new_doc = '''    doc = report_tool.build_doc(
        ticker=_ticker,
        name=name,
        template=template,
        summary=draft_summary or summary,'''
assert src.count(old_doc) == 1, "doc summary"
src = src.replace(old_doc, new_doc)

# 1h. report_system_prompt 需要包含摘要——在 prompts.py 里改

# 1i. 整篇路径加首字宽限（推理模型思考阶段静默）
old_timeout = '''    try:
        async with asyncio.timeout(_REPORT_TIMEOUT + 120.0):  # 外层：排队不计入生成预算
            async with _LLM_GATE:
                async with asyncio.timeout(_REPORT_TIMEOUT):'''
new_timeout = '''    model_name = str((state.get("llm_config") or {}).get("model") or "")
    gen_budget = 120.0 if is_reasoning_model(model_name) else _REPORT_TIMEOUT
    try:
        async with asyncio.timeout(gen_budget + 120.0):  # 外层：排队不计入生成预算
            async with _LLM_GATE:
                async with asyncio.timeout(gen_budget):'''
assert src.count(old_timeout) == 1, "timeout"
src = src.replace(old_timeout, new_timeout)

ast.parse(src)
open(p, "w", encoding="utf-8", newline="").write(src)
print("report.py: summary merged into whole-report stream")

# ════════ 2. prompts.py: 提纲首位插入投资摘要 ════════
p2 = r"E:\jingyuan\langchain\backend\src\finresearch\agents\prompts.py"
src2 = open(p2, encoding="utf-8").read()

old_outline = '''    outline = "\\n".join(
        f"【部分{i}】标题必须写作「{title}」\\n{SECTION_TASK.get(key, '')}"
        for i, (key, title) in enumerate(titles, start=1)
    )'''
new_outline = '''    # 摘要并入整篇流的首个分段
    full_titles = [("summary", "投资摘要")] + list(titles)
    outline = "\\n".join(
        f"【部分{i}】标题必须写作「{title}」\\n{SECTION_TASK.get(key, '')}"
        for i, (key, title) in enumerate(full_titles, start=1)
    )'''
if src2.count(old_outline) == 1:
    src2 = src2.replace(old_outline, new_outline)

# 给 summary 加撰写任务
old_section_task = '''SECTION_TASK = {'''
new_section_task = '''SECTION_TASK = {
    "summary": "【投资摘要撰写任务】用 3-5 句话概括本报告最核心的结论与关键数字，"
    "让读者 30 秒内抓住要点。引用事实块中的具体数字并标注口径。",'''
if old_section_task in src2 and '"summary"' not in src2.split(old_section_task)[1][:200]:
    src2 = src2.replace(old_section_task, new_section_task, 1)

ast.parse(src2)
open(p2, "w", encoding="utf-8", newline="").write(src2)
print("prompts.py: summary section added to outline")

# ════════ 3. research_tools.py: 去掉独立摘要生成 ════════
p3 = r"E:\jingyuan\langchain\backend\src\finresearch\agents\deep\research_tools.py"
src3 = open(p3, encoding="utf-8").read()

old_sum = '''    sum_out = await summary_node(state)'''
new_sum = '''    # 摘要不再独立生成——已并入 report_node 的整篇流（第一个分段），
    # 从摘要首字到报告结尾一条 LLM 流连续输出
    sum_out = {"draft_summary": ""}'''
assert src3.count(old_sum) == 1, "compose_report summary"
src3 = src3.replace(old_sum, new_sum)

ast.parse(src3)
open(p3, "w", encoding="utf-8", newline="").write(src3)
print("research_tools.py: standalone summary removed")

# ════════ 4. stream.py: 转发 summary_token 事件 ════════
p4 = r"E:\jingyuan\langchain\backend\src\finresearch\agents\deep\stream.py"
src4 = open(p4, encoding="utf-8").read()

old_custom = '''            elif kind == "section_token":'''
new_custom = '''            elif kind == "summary_token":
                # 摘要逐 token 流（与 section_token 同理，前端打字机追加到摘要卡）
                yield sse.sse({
                    "type": "summary_token",
                    "text": data.get("text", ""),
                }), ""
            elif kind == "section_token":'''
assert src4.count(old_custom) == 1, "stream summary_token"
src4 = src4.replace(old_custom, new_custom)

ast.parse(src4)
open(p4, "w", encoding="utf-8", newline="").write(src4)
print("stream.py: summary_token forwarding added")

# ════════ 5. routes.py: coalescing + summary_streamed 条件 ════════
p5 = r"E:\jingyuan\langchain\backend\src\finresearch\api\routes.py"
src5 = open(p5, encoding="utf-8").read()

# summary_streamed 条件扩展
old_ss = '''            if _ev.get("type") == "summary":
                summary_streamed = True'''
new_ss = '''            if _ev.get("type") in ("summary", "summary_token"):
                summary_streamed = True'''
if src5.count(old_ss) == 1:
    src5 = src5.replace(old_ss, new_ss)

# coalescing 也要处理 summary_token
old_coal = '''    if (
        payload is not None
        and payload.get("type") == "section_token"
        and history
    ):'''
new_coal = '''    if (
        payload is not None
        and payload.get("type") in ("section_token", "summary_token")
        and history
    ):'''
if src5.count(old_coal) == 1:
    src5 = src5.replace(old_coal, new_coal)

# 里面匹配 last 也要包含 summary_token
old_last = '''        if (
            isinstance(last, dict)
            and last.get("type") == "section_token"
            and last.get("title") == payload.get("title")
        ):'''
new_last = '''        if (
            isinstance(last, dict)
            and last.get("type") in ("section_token", "summary_token")
            and (last.get("title") == payload.get("title") or last.get("type") == payload.get("type") == "summary_token")
        ):'''
if src5.count(old_last) == 1:
    src5 = src5.replace(old_last, new_last)

ast.parse(src5)
open(p5, "w", encoding="utf-8", newline="").write(src5)
print("routes.py: coalescing + summary_streamed updated")

print("ALL BACKEND CHANGES APPLIED")
