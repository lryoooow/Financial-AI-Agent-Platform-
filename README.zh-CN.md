<div align="center">

<img src="assets/logo-256.png" width="120" alt="景渊">

# 景渊 JingYuan

[![下载](https://img.shields.io/badge/下载-v2.7.0-blue?style=for-the-badge&logo=github)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)

[English](README.md) · **简体中文**

</div>

---

**景渊是一款隐私优先的 A 股 AI 研究助手。** 它完全运行在你自己的电脑上，抓取真实行情数据、用零编造数字撰写研究报告、记住你聊过的每一次对话——所有数据不上传任何服务器。

它是研究工具，不是交易工具。不接券商，不下单，永远不。

---

## 你能做什么

### 💬 问它任何问题

> "茅台现在多少钱？" → 秒回，数据带时点
>
> "茅台和五粮液比一下" → 拉真实数据，并排对比
>
> "白酒行业估值高吗？" → 行业 PE / PB / 股息率
>
> "我之前看好什么来着？" → 它检索你的历史对话来回答

### 🧠 它记得你

你说过的话、生成的研究报告、上传的文档——都会成为它的记忆。下次对话它会引用你之前的内容。

### 📊 看盘

打开就是五大指数实时行情。点进任何股票看分时、K线、五档盘口、技术指标。

### 📝 生成研究报告

选标的、选模板，智能体自动取数、从第一个字写到最后一个字连续流式输出。导出 Word / PDF / Markdown。

### 📂 建立你的知识库

上传你自己的研究文档（PDF / Word / Markdown / TXT），智能体在对话中结合实时行情检索引用。

---

## 快速开始

1. 下载安装
2. 进入**模型配置** → 选厂商 → 粘贴 API Key
3. 开始提问

**Windows 10/11 x64** · 无需另装 Python / Node · SmartScreen 拦截时点"仍要运行"

---

## 为什么本地运行

你的对话、文档、报告、API Key——全部只存在 `%APPDATA%\FinResearch\`。

没有云端、没有账号、没有追踪。唯一的外发请求发往你自己选的模型厂商。

---

## 三条铁律

| 铁律 | 含义 |
|------|------|
| **数字零幻觉** | 只用真实接口。取不到显示"—"，绝不编 0.0 |
| **可溯源** | 标明数据来源、哪段是 AI 写的、哪段是模板 |
| **仅供参考** | 不出现"买入/卖出"指令词，强制带免责声明 |

---

## 技术栈

Electron · FastAPI · LangChain / LangGraph · deepagents · SQLite · sqlite-vec

[更新日志](CHANGELOG.md) · [数据结构](backend/DATA_LAYOUT.md)

---

<div align="center">

*景渊——看得远，也看得深。*

*研究工具，不构成投资建议。*

</div>
