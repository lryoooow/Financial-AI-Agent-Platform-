"""单次流式研报的增量分段解析（v1.8.0）。

一次 LLM 调用按序写「投资摘要 → 各分析段」，正文以「## 标题」行分段。本模块把 token 流
增量地切到各部分：

- 只有**行首**、且到目前为止仍可能是标题的字符才会被暂存（`#`/`【`/`*` 开头，或是某个标题
  的前缀）；其余字符立即放行 —— 正文逐 token 外发，不会被整行缓冲拖慢；
- 标题匹配有容错：`## 技术面分析`、`**技术面分析**`、`【技术面分析】`、`二、技术面分析：`
  都能认出；只允许向后跳（重复标题/回跳标题行被丢弃，正文不串段）；
- 第一个标题出现前的正文归入第 0 部分（模型省略了「## 投资摘要」也不丢摘要）。

纯函数式、无 IO，便于单测。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

# 事件：("enter", idx, "") 进入第 idx 部分；("text", idx, s) 第 idx 部分的正文增量
Event = Tuple[str, int, str]

_LEAD = re.compile(r"^[\s#*＃【\[〔〖（(]*")
_LEAD_OPEN_PAREN = re.compile(r"^[\s（(]*")
_NUMBERING = re.compile(
    r"^(?:第?[一二三四五六七八九十百]+(?:部分|章节|章|节|篇|段)?[、.．)）:：\s]*|\d{1,2}(?:[、.．)）:：]|\s)\s*)"
)
_TRAIL = re.compile(r"[\s*】\]〕〗）):：#＃]*$")
_DECOR_START = ("#", "＃", "【", "*", "[", "〔", "〖")
_STRUCTURAL_START = ("#", "＃", "【", "[", "〔", "〖")
# 行首只有编号（「1」「1.」「二、」「（一）」「第三部分」「第一节」）：可能是编号标题的开头，继续观察
_PENDING_NUM = re.compile(
    r"^(?:第[一二三四五六七八九十百]*(?:部分?|章节?|章|节|篇|段)?|[一二三四五六七八九十百]+(?:部分?|章节?|章|节|篇|段)?|\d{1,2})[、.．)）:：]?\s*$"
)
# 标题允许的后缀只有括注（「技术面分析（短期）」）；「**技术面分析**：偏弱」这类加粗引导句不是标题
_PAREN_SUFFIX = re.compile(r"^[（(][^（()）]{0,16}[）)]?$")  # 收尾括号可能已被 _TRAIL 剥掉


def _core(line: str) -> str:
    s = _LEAD.sub("", line.strip())
    s = _NUMBERING.sub("", s)
    s = _LEAD.sub("", s)
    return _TRAIL.sub("", s).strip()


def _is_structural_heading(stripped: str) -> bool:
    """是否为强结构化标题（Markdown #、方括号【】、或带显式章节序号）。

    裸文字或单纯 **加粗短语** 仅在顺承进入紧邻下一章（idx + 1）时容忍，
    不允许跨章跳段（j > idx + 1）或触发回跳丢弃（j < idx），防止正文小结误吞整章。
    """
    if stripped.startswith(_STRUCTURAL_START):
        return True
    after_paren = _LEAD_OPEN_PAREN.sub("", stripped)
    return bool(_NUMBERING.match(after_paren))


class SectionStreamParser:
    """增量解析器。titles[i] 是第 i 部分可接受的标题别名（首个为规范标题）。"""

    def __init__(self, titles: Sequence[Iterable[str]]) -> None:
        self._aliases: List[Tuple[str, ...]] = [tuple(a for a in t if a) for t in titles]
        self._maxlen = max((len(a) for t in self._aliases for a in t), default=8) + 26
        self.idx = 0
        self._hold = ""
        self._at_line_start = True
        # 模型回跳写了更早部分的标题：其后正文丢弃直到下一个向后的标题（宁缺勿串段，
        # 被跳过的部分由调用方回退模板）
        self._discard = False

    # ── 标题判定 ──────────────────────────────────────────
    def _match(self, line: str) -> tuple[int, bool]:
        """整行是否为某部分标题；返回 (下标, 是否强结构标题)，失配返回 (-1, False)。"""
        stripped = line.strip()
        if not stripped or len(stripped) > self._maxlen:
            return -1, False
        core = _core(stripped)
        if not core:
            return -1, False
        decorated = stripped.startswith(_DECOR_START)
        structural = _is_structural_heading(stripped)
        for i, aliases in enumerate(self._aliases):
            for a in aliases:
                if core == a:
                    return i, structural
                # 「技术面分析（短期）」这类带括注的标题：仅在有标题装饰或结构序号时接受
                if (decorated or structural) and core.startswith(a) and _PAREN_SUFFIX.match(core[len(a):].strip()):
                    return i, structural
        return -1, False

    def _could_be_heading(self, s: str) -> bool:
        stripped = s.lstrip()
        if not stripped:
            return len(s) <= 4  # 行首少量空白：继续观察
        if len(stripped) > self._maxlen:
            return False
        after_lead = _LEAD.sub("", stripped)
        if not after_lead or _PENDING_NUM.match(after_lead):
            return True  # 仅含装饰符或未写完的编号（如「## 」「**」「（一）」「第一节」）
        core = _core(stripped)
        if not core:
            return True
        decorated = stripped.startswith(_DECOR_START) or _is_structural_heading(stripped)
        for aliases in self._aliases:
            for a in aliases:
                if a.startswith(core) or core == a:
                    return True
                if decorated and core.startswith(a):
                    rest = core[len(a):].strip()
                    if not rest or _PAREN_SUFFIX.match(rest):
                        return True
        return False

    # ── 喂入 ─────────────────────────────────────────────
    def feed(self, text: str) -> List[Event]:
        out: List[Event] = []
        run: List[str] = []

        def flush_run() -> None:
            if run and not self._discard:
                _emit_text(out, self.idx, "".join(run))
            run.clear()

        for ch in text or "":
            if not self._at_line_start:
                run.append(ch)
                if ch == "\n":
                    self._at_line_start = True
                continue
            # 行首模式：判断这一行是不是标题
            if ch == "\n":
                line, self._hold = self._hold, ""
                j, structural = self._match(line)
                if j == self.idx + 1 or (j > self.idx + 1 and structural):
                    flush_run()
                    self.idx = j
                    self._discard = False
                    out.append(("enter", j, ""))
                elif j == self.idx and (structural or line.strip().startswith(_DECOR_START)):
                    pass  # 当前部分的重复结构标题行（含开头的「## 投资摘要」）：丢弃
                elif 0 <= j < self.idx and structural:
                    flush_run()
                    self._discard = True  # 强结构标题回跳到更早部分：丢弃其正文，不串进当前部分
                else:
                    run.append(line + "\n")
                continue
            self._hold += ch
            if not self._could_be_heading(self._hold):
                run.append(self._hold)
                self._hold = ""
                self._at_line_start = False
        flush_run()
        return out

    def flush(self) -> List[Event]:
        """流结束：处理最后一行（可能没有换行符）。"""
        out: List[Event] = []
        if self._hold:
            line, self._hold = self._hold, ""
            j, structural = self._match(line)
            if j == self.idx + 1 or (j > self.idx + 1 and structural):
                self.idx = j
                self._discard = False
                out.append(("enter", j, ""))
            elif j < 0 and not self._discard:
                _emit_text(out, self.idx, line)
            elif not structural and j != self.idx and not self._discard:
                _emit_text(out, self.idx, line)
        self._at_line_start = True
        return out


def _emit_text(out: List[Event], idx: int, s: str) -> None:
    if not s:
        return
    if out and out[-1][0] == "text" and out[-1][1] == idx:
        out[-1] = ("text", idx, out[-1][2] + s)
    else:
        out.append(("text", idx, s))
