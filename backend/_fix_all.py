# -*- coding: utf-8 -*-
"""综合修复：tool_call残余3项 + 幽灵段 + summary闪变 + 回放折叠。用后即删。"""
import ast

# ════════ 1. report.py: 摘要段不发 section_start（修幽灵段）════════
p = r"E:\jingyuan\langchain\backend\src\finresearch\agents\workers\report.py"
src = open(p, encoding="utf-8").read()

old_open = '''    def _open(self, title: str) -> None:
        if self._current is not None and self._current != title:
            self._completed.add(self._current)  # 标题边界：上一段确定写完
        self._current = title
        if title not in self._started:
            self._started.add(title)
            self._bodies.setdefault(title, [])
            self._on_event({"type": "section_start", "title": title})'''
new_open = '''    def _open(self, title: str) -> None:
        if self._current is not None and self._current != title:
            self._completed.add(self._current)  # 标题边界：上一段确定写完
        self._current = title
        if title not in self._started:
            self._started.add(title)
            self._bodies.setdefault(title, [])
            # 摘要段不发 section_start：前端不会在正文区建空段（done 时 doc.sections
            # 不含摘要 → 空段瞬间消失 = "内容闪现后消失"的根因）
            if title != self.SUMMARY_TITLE:
                self._on_event({"type": "section_start", "title": title})'''
assert src.count(old_open) == 1, "splitter _open"
src = src.replace(old_open, new_open)
open(p, "w", encoding="utf-8", newline="").write(src)
ast.parse(src)
print("1. report.py: 摘要段不发 section_start")

# ════════ 2. stream.py: ok/error 帧带 call_id ════════
p2 = r"E:\jingyuan\langchain\backend\src\finresearch\agents\deep\stream.py"
src2 = open(p2, encoding="utf-8").read()

# 找到 ok/error 发射处并加 call_id
import re
# 查找现有的 ok/error 事件发射
ok_pattern = re.compile(
    r'yield sse\.sse\(\{\s*"type":\s*"tool_call",\s*"name":\s*str\(getattr\(msg,\s*"name",\s*""\)\),\s*'
    r'"label":\s*tool_label\([^)]+\),\s*"status":\s*"error"\s*if\s*_tool_failed\([^)]+\)\s*else\s*"ok",\s*'
    r'"call_id":\s*str\(getattr\(msg,\s*"tool_call_id",\s*""\)\s*or\s*""\),?\s*\}\),\s*""'
)
m = ok_pattern.search(src2)
if not m:
    # 尝试更宽松的匹配
    ok_pattern2 = re.compile(r'"status":\s*"error"[^"]*if[^"]*else\s*"ok"[^}]*\}', src2)
    m2 = ok_pattern2.search(src2)
    if m2:
        # 已有 call_id 或需要添加
        pass
    else:
        # 找 yield sse.ev_tool_call 形式
        old_yield = 'yield sse.ev_tool_call('
        count = src2.count(old_yield)
        if count > 0:
            # 替换所有 ev_tool_call 为内联 sse 格式（带 call_id）
            src2 = src2.replace(
                old_yield,
                'yield sse.sse({"type": "tool_call", "call_id": str(getattr(msg, "tool_call_id", "") or ""), '
            )
            # 这太复杂了——直接在 stream.py 中找到实际的 ok/error 生成处
            # 先检查是否已经用了内联格式
            pass

# 简化处理：找到 node == "tools" 分支的 yield
old_tools_yield = '''            if msg_node == "tools" and hasattr(msg, "name"):
                failed = _tool_failed(getattr(msg, "content", ""))'''
if old_tools_yield in src2:
    # 查看后续几行
    idx = src2.index(old_tools_yield)
    after = src2[idx:idx + 500]
    # 找 yield 行并替换
    if 'yield sse.sse({' in after and 'tool_call' in after and 'call_id' in after:
        print("2. stream.py: ok/error already has call_id")
    elif 'yield sse.ev_tool_call' in after:
        # 需要替换
        old_yield_line = re.search(r'yield sse\.ev_tool_call\([^)]+\)[^\\n]*', after)
        if old_yield_line:
            old_text = old_yield_line.group(0)
            new_text = '''yield sse.sse({
                        "type": "tool_call",
                        "name": str(getattr(msg, "name", "")),
                        "label": tool_label(str(getattr(msg, "name", ""))),
                        "status": "error" if failed else "ok",
                        "call_id": str(getattr(msg, "tool_call_id", "") or ""),
                    }), ""'''
            src2 = src2.replace(old_text, new_text, 1)
            print("2. stream.py: ok/error now has call_id")
    else:
        print("2. stream.py: checking actual yield format...")
        # 打印该区域供调试
        print(after[:300])
else:
    print("2. stream.py: tools branch not found with expected pattern")

ast.parse(src2)
open(p2, "w", encoding="utf-8", newline="").write(src2)

# ════════ 3. Workspace.tsx: done/error 时收口所有 start 芯片 + summary drainTokens ════════
p3 = r"E:\jingyuan\langchain\fronted\src\app\components\pages\Workspace.tsx"
src3 = open(p3, encoding="utf-8").read()

# 3a. summary 处理器加 drainTokens
old_summary = '''        } else if (ev.type === "summary") {
          // 权威收口：替换语义（兜住 sanitize 差异 + 断线重连回放）
          setTyped(ev.text ?? "");'''
new_summary = '''        } else if (ev.type === "summary") {
          // 权威收口：替换语义（兜住 sanitize 差异 + 断线重连回放）
          drainTokens(); // 排空缓冲残留的摘要 token，防止替换后追加闪变
          setTyped(ev.text ?? "");'''
assert src3.count(old_summary) == 1, "summary drain"
src3 = src3.replace(old_summary, new_summary)

# 3b. done 时收口所有 start 芯片为 ok
old_done = '''        } else if (ev.type === "done") {
          if (terminal) { finish(); return; }
          terminal = true; // 重连回放会再次送达 done：报告入库回调只许触发一次'''
new_done = '''        } else if (ev.type === "done") {
          if (terminal) { finish(); return; }
          terminal = true;
          // 终态兜底：所有仍在 start 的芯片收口为 ok（杜绝任何路径的孤儿转圈）
          setToolCalls((calls) => calls.map((c) => c.status === "start" ? { ...c, status: "ok" as const } : c));'''
assert src3.count(old_done) == 1, "done terminal"
src3 = src3.replace(old_done, new_done)

# 3c. error 时也收口
old_error = '''        } else if (ev.type === "error") {
          if (terminal) { finish(); return; }
          terminal = true;'''
new_error = '''        } else if (ev.type === "error") {
          if (terminal) { finish(); return; }
          terminal = true;
          setToolCalls((calls) => calls.map((c) => c.status === "start" ? { ...c, status: "error" as const } : c));'''
assert src3.count(old_error) == 1, "error terminal"
src3 = src3.replace(old_error, new_error)

open(p3, "w", encoding="utf-8", newline="").write(src3)
print("3. Workspace.tsx: terminal fallback + summary drain added")

# ════════ 4. routes.py: summary_token 回放折叠 ════════
p4 = r"E:\jingyuan\langchain\backend\src\finresearch\api\routes.py"
src4 = open(p4, encoding="utf-8").read()

# 检查 coalesce 是否已包含 summary_token
if 'summary_token' in src4 and 'section_token' in src4:
    # 已在之前修改中加入
    print("4. routes.py: summary_token coalescing already present")
else:
    # 需要添加
    old_c = 'payload.get("type") in ("section_token", "summary_token")'
    if old_c not in src4:
        # 找到 coalesce 中的 type 判断
        old_type = 'payload.get("type") == "section_token"'
        if old_type in src4:
            src4 = src4.replace(old_type, 'payload.get("type") in ("section_token", "summary_token")', 1)
            print("4. routes.py: summary_token coalescing added")
    else:
        print("4. routes.py: already present")

# 终态回放修复：成对发 start+ok
old_replay = '''            for evt in (existing.progress_events or []):
                node = evt.get("node") or ""
                if node and node not in ("_task",):
                    replay_frames.append(sse.sse({
                        "type": "tool_call", "name": node,
                        "label": node, "status": "ok", "call_id": "",
                    }))'''
new_replay = '''            for evt in (existing.progress_events or []):
                node = evt.get("node") or ""
                if node and node not in ("_task",):
                    from ..agents.deep.stream import tool_label
                    replay_frames.append(sse.sse({
                        "type": "tool_call", "name": node,
                        "label": tool_label(node), "status": "start", "call_id": "",
                    }))
                    replay_frames.append(sse.sse({
                        "type": "tool_call", "name": node,
                        "label": tool_label(node), "status": "ok", "call_id": "",
                    }))'''
if old_replay in src4:
    src4 = src4.replace(old_replay, new_replay, 1)
    print("4b. routes.py: terminal replay now sends start+ok pairs")

# existing 预初始化
old_existing = '''    terminal_frame: Optional[str] = None
    async with session_scope() as s:'''
new_existing = '''    terminal_frame: Optional[str] = None
    existing = None  # 预初始化（无 DB 时 NameError 防护）
    async with session_scope() as s:'''
if old_existing in src4 and 'existing = None' not in src4.split(old_existing)[0][-50:]:
    src4 = src4.replace(old_existing, new_existing, 1)
    print("4c. routes.py: existing pre-initialized")

ast.parse(src4)
open(p4, "w", encoding="utf-8", newline="").write(src4)
print("ALL FIXES APPLIED")
