<div align="center">

<img src="assets/logo-256.png" width="112" alt="JingYuan logo">

# 景渊 JingYuan

**A local-first A-share AI research copilot for individual investors.**

[![Release](https://img.shields.io/badge/release-v1.8.1-D8B24A)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0B1220)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![License](https://img.shields.io/badge/license-Personal%20use%20only-8B5CF6)](LICENSE)

**English** · [简体中文](README.zh-CN.md)

</div>

---

## What it is

JingYuan runs entirely on your own machine. It pulls real A-share market data, financial
statements and news, computes every number locally, and asks a large language model to *analyze
and write about* those numbers — never to invent them.

It is a research tool, not a trading tool. It has no broker integration and never will.

> **景渊** — *jǐng* (vista, prospect) + *yuān* (a deep pool). Look far, look deep.

## Three rules the code enforces

These are not slogans in a doc. Each one has an execution point and automated tests behind it.

| Rule | What it means in practice |
| --- | --- |
| **Zero fabricated numbers** | Every figure in a report comes from a structured fact block computed by local tools. If a data source fails, the field renders as `—` (unavailable) — never as `0.0`. Insufficient moving-average windows stay unplotted rather than interpolated. |
| **Traceable** | Each report states which data source served it, its timestamp, which paragraphs the model wrote, and which fell back to rule-based templates. Quoted headlines are reproduced verbatim and never rewritten. |
| **Advisory only** | The model is forbidden from issuing directive trading advice (buy / sell / add / reduce / stop-loss …). Output passes a compliance guard; violations trigger a rewrite or template fallback. Every report carries a mandatory disclaimer. |

If the market snapshot or valuation is unavailable, the report states so and displays `—` —
rather than quietly printing `PE 0.0`.

## Core Workspaces

- **Deep Research Workbench** — Dedicated flagship workspace on the top of the sidebar. Pick any A-share stock/ETF (by ticker, Chinese name, or pinyin initials such as `gzmt` → Kweichow Moutai) or Shenwan industry ("半导体", "光伏"), choose from 4 structured templates (*Equity Deep-Dive*, *Industry*, *Earnings Review*, *Risk Assessment*), and attach custom focus questions. The agent fetches multi-source facts in parallel and **streams the entire report continuously token by token** from executive summary to final section, with live reasoning and tool traces, one-click cancellation, and background execution.
- **Research Q&A (Multi-turn & Vision)** — Ask in natural language ("Compare Moutai and Wuliangye"), attach chart screenshots for multimodal technical reading, and follow up across long sessions backed by automatic context compaction.
- **Live Market & Chart Quick-Read** — Intraday curves, daily candlesticks (MA5/10/20 + full OHLCV tooltip), 5-level bid/ask order book, technical indicators (MACD / RSI / KDJ / BOLL), auto-refresh during trading hours, and hover AI quick-read.
- **Report Library & Local Knowledge Base (RAG)** — Every generated report is stored in local SQLite and indexed into an offline FTS5 knowledge base so future Q&A can cite your past research with report dates. Export to **Word (`.docx`), PDF, HTML, Markdown, or TXT**, or permanently delete reports along with their tasks, knowledge chunks, and checkpoints.
- **Settings & Personalization** — 15+ preset domestic and global OpenAI-compatible providers plus custom relay endpoints, live `/v1/models` discovery, reasoning-effort tiers (Default / Low / Medium / High), optional preference learning, and system-tray background persistence.

## Architecture

```
┌──────────────────────── Your PC (Windows) ─────────────────────────┐
│                                                                    │
│  Electron shell — frameless window, title bar, system tray         │
│        │  loads http://127.0.0.1:<port>/                           │
│        ▼                                                           │
│  FastAPI backend (PyInstaller sidecar, serves the frontend)        │
│    • deepagents agent (LangChain/LangGraph), SQLite checkpoints    │
│    • Market tools: eastmoney-direct → sina → tencent → akshare     │
│    • Local SQLite + FTS5 knowledge base (chats, reports, settings) │
│        │                                                           │
│        │  HTTPS — the only outbound hop                            │
│        ▼                                                           │
│  Cloud LLM API — your key, your chosen vendor                      │
└────────────────────────────────────────────────────────────────────┘
```

Your questions, watchlist and generated reports never leave your machine. Market data comes
from public endpoints. The only request carrying your API key goes to the LLM vendor you chose.

## Install

Download from the [**latest release**](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest):

| File | Use |
| --- | --- |
| `JingYuan-Setup-1.8.1.exe` | Installer (recommended). Creates Start-menu and desktop shortcuts; upgrades preserve local data. |
| `JingYuan-Portable-1.8.1.exe` | Portable. Runs directly without installation. |

Windows 10/11, x64. No Python or Node runtime required — everything is bundled.

The binaries are unsigned, so Windows SmartScreen may warn on first launch. Choose **More info →
Run anyway** if you trust this source.

## First run

1. Launch JingYuan. The local research engine starts automatically and opens **深度研究** (Deep Research).
2. Open **设置 → 模型服务** (Settings → Model Providers), choose a vendor, paste your API key, pick a model, and click **设为当前推理模型** (Set as active model).
3. Launch your first report in **深度研究** or start asking questions in **投研问答** (Research Q&A).

Without an API key, live quotes, charts, order books and diagnostics still work — report prose falls
back to deterministic rule-based templates.

## Where your data lives

Everything sits in `%APPDATA%\FinResearch\`:

- `finresearch.db` — conversations, reports, encrypted model configs, local settings
- `knowledge.db` — offline FTS5 knowledge chunks and learned user profile
- `checkpoints.sqlite` — LangGraph thread checkpoints
- `backend.log` / `backend-sidecar.log` — rotating backend logs
- `port.txt` — the local port bound by the backend

To reset the app completely, quit from the system tray icon and delete that folder. To back up, copy it.

API keys are encrypted at rest in the local database via **native Windows DPAPI**, masked across UI and local endpoints, and transmitted only to the LLM
vendor you selected.

## Disclaimer

JingYuan is a research aid. It does not provide investment advice, does not connect to any broker,
and cannot place orders. Model output may be wrong or incomplete. Market data comes from free public
sources and may be delayed, revised, or unavailable. **Every investment decision, and every
consequence of it, is yours.** Markets carry risk.

## License

Free for **personal, non-commercial use**. Commercial use, redistribution and reverse engineering
are prohibited. See [LICENSE](LICENSE) and [CHANGELOG](CHANGELOG.md).

---

<div align="center">
<sub>景渊 JingYuan · Built for research, not for trading.</sub>
</div>
