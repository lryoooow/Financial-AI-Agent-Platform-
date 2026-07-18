import React from "react";

/**
 * 对话回复的"结构化纯文本"渲染器（取代 Markdown 渲染——彻底杜绝 .md）。
 *
 * 设计（对应用户反馈 #2/#4/#5）：
 * - 后端已要求模型输出【固定标签】分段的纯文本（无星号加粗、井号标题、反引号、减号列表），并已 _strip_md 清洗。
 * - 这里再按"段落标签"做确定性视觉强调：把 【核心结论】【关键依据】【风险提醒】【深入追问】 等
 *   标签渲染成金色徽章 + 后随文本；· / 1) 2) 渲染成列表；其余为段落。emoji 原样保留。
 * - 防御层：即便模型/网络偶发带回 **、`、#，也在此再清一遍，保证界面永不出现裸 .md。
 *
 * 不使用 dangerouslySetInnerHTML（自动转义，无 XSS）。
 */

/**
 * 只删「处在语法位置」的 Markdown 符号，不做无差别字符删除。
 *
 * 旧实现结尾是 .replace(/\*​/g,"").replace(/#/g,"")——它把 3*5 变成 35、
 * 把「排名 #1」变成「排名 1」。本函数总是作用于**已累积的完整消息文本**
 * （每个 token 到达后整条重新渲染），所以 ** 这类成对标记必然完整，
 * 无需靠删字符来兜底。末尾的半截标记单独裁掉，避免流式过程中闪现 `**`。
 */
function defensiveClean(text: string): string {
  return text
    .replace(/```[A-Za-z0-9_+-]*\n?/g, "")                 // 代码块围栏（含语言标注）
    .replace(/`([^`\n]+)`/g, "$1")                         // `code` → code
    .replace(/\*\*(?=\S)([\s\S]+?)(?<=\S)\*\*/g, "$1")     // **粗** → 粗
    .replace(/__(?=\S)([\s\S]+?)(?<=\S)__/g, "$1")
    .replace(/(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])/g, "$1")  // *斜* → 斜，放过 3*5
    .replace(/(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])/g, "$1")    // 放过 snake_case
    .replace(/^[ \t]{0,3}#{1,6}(?:[ \t]+|(?=[^\s\d#]))/gm, "")         // # 标题，放过「#1 排名」
    .replace(/^[ \t]*[-*+][ \t]+/gm, "· ")                 // 行首 - / * / + 列表 → ·
    .replace(/[ \t]*(?:\*{1,2}|_{1,2}|`{1,3})[ \t]*$/, ""); // 流式尾部的半截标记（不含 #，保住「C#」）
}

export function StructuredText({ content, className }: { content: string; className?: string }) {
  const lines = defensiveClean(content || "").replace(/\r\n/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let list: { tag: "ul" | "ol"; items: string[] } | null = null;
  let key = 0;

  const flushList = () => {
    if (!list) return;
    const Tag = list.tag;
    blocks.push(
      <Tag key={`l-${key++}`} className={Tag === "ul" ? "my-1.5 list-disc pl-5 space-y-0.5" : "my-1.5 list-decimal pl-5 space-y-0.5"}>
        {list.items.map((it, i) => <li key={i}>{it}</li>)}
      </Tag>
    );
    list = null;
  };

  for (const line of lines) {
    // 标签行：【label】 可带前导 emoji，后随该段内容（可空）
    const label = /^\s*【([^】]+)】(.*)$/.exec(line);
    if (label) {
      flushList();
      const rest = label[2].trim();
      blocks.push(
        <p key={`lb-${key++}`} className="mt-2.5 mb-0.5 leading-relaxed">
          <span className="inline-block px-1.5 py-0.5 mr-1.5 rounded bg-gold/15 text-gold text-[11px] font-semibold align-middle">
            【{label[1]}】
          </span>
          {rest && <span className="align-middle">{rest}</span>}
        </p>
      );
      continue;
    }
    // · / • 无序列表
    if (/^\s*[·•]\s+/.test(line)) {
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(line.replace(/^\s*[·•]\s+/, ""));
      continue;
    }
    // 1) / 1. / 1、 有序列表
    if (/^\s*\d+[.)、]\s+/.test(line)) {
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(line.replace(/^\s*\d+[.)、]\s+/, ""));
      continue;
    }
    if (line.trim() === "") { flushList(); continue; }
    flushList();
    blocks.push(<p key={`p-${key++}`} className="my-1.5 leading-relaxed">{line}</p>);
  }
  flushList();

  return <div className={className}>{blocks}</div>;
}
