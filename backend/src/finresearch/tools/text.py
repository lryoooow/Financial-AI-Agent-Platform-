"""模型输出的纯文本清洗。

对话链路与报告链路共用：两边的输出规范都写着"禁止 Markdown"，但提示词是软约束，
模型总有不听话的时候。清洗才是硬保证——尤其是报告，前端用 whitespace-pre-wrap
纯文本渲染，漏出去的 `**加粗**` 会原样显示成字面符号。

此前这份实现只存在于 api/routes.py（对话链路私有），报告链路没有对应清洗，
于是"杜绝 .md"这条规矩实际只有一半生效。搬到这里让两条链路共用同一份。

emoji 不在清洗范围内：下面每条正则都只匹配 Markdown 语法符号。
报告与对话的输出规范都明确允许 📊💡⚠️ 作克制点缀。
"""

from __future__ import annotations

import re

# Markdown 清洗：只删「处在语法位置」的符号，不做无差别字符删除。
#
# 旧实现最后三行是 text.replace("*","").replace("`","").replace("#","")——
# 它把 3*5 变成 35、把「排名 #1」变成「排名 1」。之所以敢这么写，是因为它被**逐 token**
# 调用：`**` 可能被切成两个 token，成对的正则永远匹配不上，只好按字符删。
#
# 正确的做法是不要逐 token 清洗：流式原样透传，前端每次渲染对「已累积的完整文本」
# 做一次防御性清洗，成对标记因此总是完整的；后端只在落库前清洗一次。
_MD_FENCE = re.compile(r"```[A-Za-z0-9_+-]*\n?")          # 代码块围栏（含语言标注）
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")              # `code` → code
_MD_BOLD = re.compile(r"\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*")  # **粗** → 粗
_MD_BOLD_U = re.compile(r"__(?=\S)([\s\S]+?)(?<=\S)__")
# 斜体：两侧不能紧邻单词字符，避免吃掉 3*5 与 snake_case
_MD_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_MD_ITALIC_U = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
# 标题：行首 #，后接空格或非数字（保住「#1 排名」）
_MD_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}(?:[ \t]+|(?=[^\s\d#]))")
_MD_BULLET = re.compile(r"(?m)^[ \t]*[-*+][ \t]+")        # 行首 - / * / + 列表
# 流式尾部的半截标记（连同它前面的空白一起裁掉）。不含 #，否则会吃掉结尾的「C#」
_MD_TRAILING = re.compile(r"[ \t]*(?:\*{1,2}|_{1,2}|`{1,3})[ \t]*$")


def strip_md(text: str) -> str:
    """把 Markdown 语法转成纯文本（"杜绝 .md"），但不误伤正文里的 * # _ ` 字符。

    只在**完整文本**上调用（落库前）。逐 token 调用会让成对标记匹配不上。
    """
    if not text:
        return text
    text = _MD_FENCE.sub("", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_BOLD_U.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_ITALIC_U.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("· ", text)
    return _MD_TRAILING.sub("", text)
