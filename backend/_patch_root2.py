# -*- coding: utf-8 -*-
"""根因2-5修复：Electron焦点/锁泄漏+看门狗/init async化/search锁。用后即删。"""
import ast

# ════════ 根因2：Electron 焦点竞态 ════════
p = r"E:\jingyuan\langchain\fronted\electron\main.cjs"
src = open(p, encoding="utf-8").read()
old = '''  win.loadURL(frontendUrl(port));
  // frameless 窗口从无前台权限的上下文启动（安装器收尾拉起、开机场景、其它全屏应用
  // 持前台锁）时会"可见但未激活"——点击无光标、打字无反应（渲染进程丢弃输入事件）。
  // 显式 show + focus 保证窗口拿到前台与键盘焦点。
  win.show();
  win.focus();
  win.on('closed', () => { win = null; });'''
new = '''  win.loadURL(frontendUrl(port));
  // frameless 窗口从无前台权限的上下文启动时会"可见但未激活"——打字无反应。
  // 焦点必须在页面加载完成后给（loadURL 是异步的，提前 focus 会被 Windows 前台
  // 权限限制丢弃；后端重启的整页 loadURL 重载同样走这里）。
  win.webContents.once('did-finish-load', () => {
    win.show();
    win.focus();
    win.webContents.focus();
  });
  // 兜底：如果 did-finish-load 3 秒后仍未触发（后端极慢），也强制 show
  setTimeout(() => { if (!win.isDestroyed()) { win.show(); } }, 3000);
  win.on('closed', () => { win = null; });'''
assert src.count(old) == 1, "electron focus"
src = src.replace(old, new)

# 重启路径也补 focus
old2 = '''        if (win && !win.isDestroyed()) {
          win.loadURL(frontendUrl(port));
          win.show();
          win.focus(); // 整页重载会丢键盘焦点，重启后必须还给用户
        }'''
new2 = '''        if (win && !win.isDestroyed()) {
          win.loadURL(frontendUrl(port));
          win.webContents.once('did-finish-load', () => {
            if (!win.isDestroyed()) { win.show(); win.focus(); win.webContents.focus(); }
          });
        }'''
assert src.count(old2) == 1, "electron restart"
src = src.replace(old2, new2)
open(p, "w", encoding="utf-8", newline="").write(src)
print("electron focus fixed")

# ════════ 根因4：main.py init_memory 改 await ════════
p2 = r"E:\jingyuan\langchain\backend\src\finresearch\main.py"
src2 = open(p2, encoding="utf-8").read()
old3 = "    memory_mod.init_memory()"
new3 = "    await memory_mod.init_memory()"
assert src2.count(old3) == 1, "main await"
src2 = src2.replace(old3, new3)
open(p2, "w", encoding="utf-8", newline="").write(src2)
ast.parse(src2)
print("main.py await fixed")

# ════════ 根因3：锁泄漏 + 看门狗 ════════
p3 = r"E:\jingyuan\langchain\backend\src\finresearch\api\routes.py"
src3 = open(p3, encoding="utf-8").read()

# 3a. 有界等待 + 409
old4 = '''    lock = _chat_lock(conversation_id)
    await lock.acquire()'''
new4 = '''    lock = _chat_lock(conversation_id)
    # 有界等待：曾经无限等待——锁泄漏（SSE 流从未被迭代时 finally 不执行）会让
    # 该会话的下一条消息永久挂起，前端 fetch 无超时 → loading 永远 true → 输入被锁
    try:
        await asyncio.wait_for(lock.acquire(), timeout=180)
    except asyncio.TimeoutError:
        raise HTTPException(409, "上一条回复仍在处理中（超过 3 分钟），请稍后重试或刷新页面。")'''
assert src3.count(old4) == 1, "lock timeout"
src3 = src3.replace(old4, new4)

# 3b. BackgroundTask 兜底释放（覆盖"从未迭代"路径）
old5 = '''    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )'''
count5 = src3.count(old5)
new5 = '''    from starlette.background import BackgroundTask

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        # 兜底释放：Starlette 在客户端断开时不一定拉取 body_iterator，
        # 从未被迭代的 async generator 的 finally 不会执行 → 锁泄漏到进程结束
        background=BackgroundTask(lambda: lock.locked() and lock.release()),
    )'''
assert count5 >= 1, f"bg task (found {count5})"
src3 = src3.replace(old5, new5, 1)  # 只替换第一个（对话链路的）
open(p3, "w", encoding="utf-8", newline="").write(src3)
ast.parse(src3)
print("routes.py lock fixes")

# ════════ 根因5：search 锁内网络调用移出 ════════
p4 = r"E:\jingyuan\langchain\backend\src\finresearch\tools\rag_store.py"
src4 = open(p4, encoding="utf-8").read()
old6 = '''        hits: List[dict[str, Any]] = []
        with self._lock:
            try:
                vec = self._serialize(embed_one(query))'''
new6 = '''        hits: List[dict[str, Any]] = []
        # 嵌入计算（供应商档下是网络调用，最长 20s）必须在锁外——曾经锁内做，
        # 所有等锁的 add/count/search 全部卡住
        try:
            vec = self._serialize(embed_one(query))
        except Exception:  # noqa: BLE001  # 嵌入失败（401/超时）→ 词法兜底
            vec = None
        with self._lock:
            try:
                if vec is not None:'''
assert src4.count(old6) == 1, "search lock"
src4 = src4.replace(old6, new6)
# 修复对应的 except：加网络异常兜底
old7 = '''            except sqlite3.Error:
                hits = []
            if not hits:'''
new7 = '''            except (sqlite3.Error, Exception):  # noqa: BLE001  # SQLite/网络异常都降词法
                hits = []
            if not hits:'''
# 只替换 search 函数里的（第一个出现的）
idx = src4.find(old7)
if idx > 0:
    src4 = src4[:idx] + new7 + src4[idx + len(old7):]
open(p4, "w", encoding="utf-8", newline="").write(src4)
ast.parse(src4)
print("rag_store search lock fixed")

# ════════ 根因3c：前端看门狗（心跳超时 abort）════════
p5 = r"E:\jingyuan\langchain\fronted\src\app\api\client.ts"
src5 = open(p5, encoding="utf-8").read()
# 在 sendChatMessageStream 里加空闲看门狗
old8 = '''  ) => {
    const r = await fetch(
      `${API_BASE}/api/chat/conversations/${encodeURIComponent(id)}/messages`,'''
new8 = '''  ) => {
    // 空闲看门狗：后端每 10s 发心跳，静默 >45s 判定为连接死掉 → abort，
    // 防止 fetch 永挂导致 loading/streaming 状态永不复位（输入被锁）
    const watchdogController = new AbortController();
    let watchdogTimer: number | null = null;
    const resetWatchdog = () => {
      if (watchdogTimer != null) window.clearTimeout(watchdogTimer);
      watchdogTimer = window.setTimeout(() => watchdogController.abort(), 45000);
    };
    resetWatchdog();
    const signal = handlers.signal ?? watchdogController.signal;
    const r = await fetch(
      `${API_BASE}/api/chat/conversations/${encodeURIComponent(id)}/messages`,'''
assert src5.count(old8) == 1, "watchdog start"
src5 = src5.replace(old8, new8)

# 在 read 循环里重置看门狗
old9 = '''      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });'''
new9 = '''      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetWatchdog(); // 每收到一帧重置（心跳也算）
        buf += decoder.decode(value, { stream: true });'''
assert src5.count(old9) == 1, "watchdog reset"
src5 = src5.replace(old9, new9)

# 函数末尾清看门狗
old10 = '''    if (!eventGate.isTerminal() && !handlers.signal?.aborted) {
      throw new Error("流式响应意外中断，正在从服务端恢复会话");
    }
  },'''
new10 = '''    if (watchdogTimer != null) window.clearTimeout(watchdogTimer);
    if (!eventGate.isTerminal() && !handlers.signal?.aborted && !watchdogController.signal.aborted) {
      throw new Error("流式响应意外中断，正在从服务端恢复会话");
    }
  },'''
assert src5.count(old10) == 1, "watchdog cleanup"
src5 = src5.replace(old10, new10)

# signal 引用替换（原来的 handlers.signal 改为合并的）
src5 = src5.replace(
    new8,
    new8.replace("handlers.signal ?? watchdogController.signal", "signal")
    .replace("const signal = signal;", "const signal = handlers.signal ?? watchdogController.signal;")
)
open(p5, "w", encoding="utf-8", newline="").write(src5)
print("client.ts watchdog added")
