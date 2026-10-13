<div align="center">

<img src="assets/logo-256.png" width="112" alt="JingYuan logo">

# 景渊 JingYuan

**A local-first A-share research copilot for serious individual investors.**

[![Release](https://img.shields.io/badge/release-v2.7.0-D8B24A)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0B1220)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![License](https://img.shields.io/badge/license-Personal%20use%20only-8B5CF6)](LICENSE)

**English** · [简体中文](README.zh-CN.md)

</div>

---

## What it is

JingYuan runs entirely on your own machine. It pulls real A-share market data, financial statements and news, computes every number locally, and asks a large language model to *write about* those numbers — never to invent them.

It remembers what you asked before, learns which stocks you watch, and builds a personal knowledge base from your own research reports — all stored locally, never uploaded.

It is a research tool, not a trading tool. It has no broker integration and never will.

> **景渊** — *jǐng* (vista, prospect) + *yuān* (a deep pool). Look far, look deep.

## Three rules the code enforces

These are not slogans in a doc. Each one has an execution point and a test behind it.

| Rule | What it means in practice |
| --- | --- |
| **Zero fabricated numbers** | Every figure in a report comes from a structured fact block computed by local tools. If a data source fails, the field renders as `—（未获取）` — never as `0.0`. |
| **Traceable** | Each report states which data source served it, which paragraphs the model wrote, and which fell back to rule-based templates. Quoted headlines are reproduced verbatim. |
| **Advisory only** | The model is forbidden from using directive verbs (buy / sell / add / reduce / stop-loss …). Output is checked and sanitized; violations trigger rewrites or template fallbacks. Every report carries a disclaimer. |

## Features

### Professional chat with memory

Ask about a company by nickname ("茅台", "宁德时代") and the agent resolves the ticker and calls tools on demand — live quotes, financials, news, technical indicators, industry valuation — citing each with an explicit timestamp.

The agent **remembers your past conversations and reports**. Ask "我之前跟你说过我看好什么板块？" and it searches its local memory to answer. Uploaded documents (txt / md / pdf / docx) become part of your personal knowledge base, searchable by the agent during conversations.

### Market analysis with A-share overview

The market page opens with a **live 5-index strip** (上证指数 / 深证成指 / 创业板指 / 科创50 / 沪深300). Click any index card to switch to its chart view. Search the full A-share universe, inspect 1-minute intraday, daily candles, volume, order book and MACD/RSI/KDJ/BOLL indicators.

### Research tasks — continuous streaming

An agent orchestrates a structured report, driving real tool calls you can watch live. The whole report — **from summary to conclusion** — is written in a **single LLM call and streams continuously**, character by character, with no pauses between sections.

Templates cover *equity deep-dive*, *industry*, *earnings review* and *risk assessment*. Length follows the selected **thinking level** (low / medium / high / max).

### Reports auto-enter your knowledge base

Every generated report is automatically chunked and embedded into the local vector database (rag.db). The model can retrieve past reports during conversations via two-level search: `search_memory` returns index cards with references, `read_memory` deep-reads the full original text.

### Report library

Reports persist in local SQLite and export to Word, PDF, TXT and Markdown. Reports can be permanently deleted — content, task, checkpoints **and vector chunks** are removed together.

### Knowledge base (user documents)

Upload your own research documents (txt / md / pdf / docx, ≤20 MB). They are parsed, chunked, embedded and made searchable by the agent. Your documents never leave the machine.

### Model configuration — 15+ providers

15 OpenAI-compatible providers preset (8 domestic, 7 international). Bring your own API key. Model lists pull live from `/v1/models`. Keys are **encrypted at rest** (machine-bound).

### Optional semantic embeddings

Five providers offer vector embedding models (通义千问 / 智谱 / 豆包 / OpenAI / Gemini). Configure one in Settings to upgrade memory retrieval from keyword matching to true semantic search. Without it, a local offline embedder works out of the box.

## Architecture

```
┌──────────────────────── Your PC (Windows) ─────────────────────────┐
│                                                                    │
│  Electron shell — frameless window, in-app title bar               │
│        │  loads http://127.0.0.1:<port>/                           │
│        ▼                                                           │
│  FastAPI backend (PyInstaller sidecar, serves the frontend)        │
│    • Parent agent (orchestration) → scenario sub-agents            │
│      (market-analysis, fundamentals-synthesis)                      │
│    • deepagents / LangChain / LangGraph, SQLite checkpoints        │
│    • Market sources: tencent → sina → eastmoney → akshare          │
│    • SQLite: finresearch.db (conversations, reports, settings)     │
│    • Vector store: rag.db (sqlite-vec, local RAG)                  │
│    • Memory lifecycle: auto-archive at 90 days, vector cleanup      │
│        │                                                           │
│        │  HTTPS — the only outbound hop                            │
│        ▼                                                           │  Cloud LLM API — your key, your chosen vendor                      │
└────────────────────────────────────────────────────────────────────┘
```

Your questions, documents and reports never leave the machine. Market data comes from public endpoints. The only request that carries your API key goes to the LLM vendor you chose.

## Install

Download from the [**latest release**](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest):

| File | Use |
| --- | --- |
| `JingYuan-Setup-2.7.0.exe` | Installer. Creates Start-menu and desktop shortcuts. |
| `JingYuan-Portable-2.7.0.exe` | Portable. Runs without installing. |

Windows 10/11, x64. No Python or Node runtime required.

The binaries are unsigned, so SmartScreen will warn on first launch. Choose **More info → Run anyway**.

## First run

1. Launch JingYuan. The backend starts automatically.
2. Go to **模型配置** (Providers), pick a vendor, paste your API key, select a model.
3. Click **设为当前推理模型**.
4. Ask a question in Chat, or generate a research report.

Without a key, market data, charts, knowledge base and diagnostics still work — only model-written prose falls back to rule-based templates.

## Where your data lives

Everything sits in `%APPDATA%\FinResearch\`:

| File / folder | What it holds |
| --- | --- |
| `finresearch.db` | Conversations, reports, tasks, settings, encrypted API keys, user profile |
| `rag.db` | Vector database (sqlite-vec): memory chunks, report chunks, knowledge-base chunks |
| `kb/` | Original copies of your uploaded documents |
| `archive/conversations/` | Compressed transcripts of auto-archived conversations (>90 days) |
| `context/` | Conversation media (images) |
| `assets/images/` | Image assets (server-side downsampled) |

**Data lifecycle**: conversations idle for 90 days are auto-summarized (LLM-generated ≤300-char summary), the original text is compressed to `archive/`, and row-level messages are deleted. Report and knowledge-base chunks are kept permanently. Chat vector chunks are cleaned at 180 days. Deleting a report removes its DB row, task, checkpoints **and** vector chunks.

To reset completely, close the app and delete that folder. To back up, copy it.

API keys are stored **encrypted** (machine-bound XOR cipher). They are transmitted only to the LLM vendor you selected.

## Disclaimer

JingYuan is a research aid. It does not provide investment advice, does not connect to any broker, and cannot place orders. Model output may be wrong or incomplete. Market data comes from free public sources and may be delayed, revised, or unavailable. **Every investment decision, and every consequence of it, is yours.** Markets carry risk.

## License

Free for **personal, non-commercial use**. Commercial use, redistribution and reverse engineering are prohibited. See [LICENSE](LICENSE).

---

<div align="center">
<sub>景渊 JingYuan · Built for research, not for trading.</sub>
</div>
