<div align="center">

<img src="assets/logo-256.png" width="112" alt="JingYuan logo">

# 景渊 JingYuan

**A local-first A-share research copilot for serious individual investors.**

[![Release](https://img.shields.io/badge/release-v1.2.2-D8B24A)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0B1220)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![License](https://img.shields.io/badge/license-Personal%20use%20only-8B5CF6)](LICENSE)

**English** · [简体中文](README.zh-CN.md)

</div>

---

## What it is

JingYuan runs entirely on your own machine. It pulls real A-share market data, financial
statements and news, computes every number locally, and asks a large language model to *write
about* those numbers — never to invent them.

It is a research tool, not a trading tool. It has no broker integration and never will.

> **景渊** — *jǐng* (vista, prospect) + *yuān* (a deep pool). Look far, look deep.

## Three rules the code enforces

These are not slogans in a doc. Each one has an execution point and a test behind it.

| Rule | What it means in practice |
| --- | --- |
| **Zero fabricated numbers** | Every figure in a report comes from a structured fact block computed by local tools. If a data source fails, the field renders as `—（未获取）` — never as `0.0`. A missing 10-day moving average is simply not mentioned, not interpolated from its neighbours. |
| **Traceable** | Each report states which data source served it, which paragraphs the model wrote, and which fell back to rule-based templates. Quoted headlines are reproduced verbatim and never rewritten. |
| **Advisory only** | The model is forbidden from using directive verbs (buy / sell / add / reduce / stop-loss …). Output is checked; a violation triggers one rewrite, then a template fallback. Every report carries a disclaimer. |

If the market snapshot fails to load, the report says so and omits every price-derived figure —
rather than quietly printing `PE 0.0`.

## Features

**Professional chat** — Ask about a company by nickname ("茅台", "宁德时代") and JingYuan resolves
the ticker, fetches a live quote and recent news, and injects them as a fact block with an explicit
timestamp. Ambiguous names ("平安" → 中国平安? 平安银行?) trigger a clarifying question instead of
a guess. When no data is available, the assistant says so rather than recalling a stale price.

**Research tasks** — A deterministic LangGraph pipeline produces a structured report:

```
entity → research → market → summary → report
```

Four templates, each assembling a different set of sections: *equity deep-dive*, *industry*,
*earnings review*, *risk assessment*. Sections without supporting data are not written — they are
declared insufficient.

**Report library** — Reports persist in local SQLite and export to Word, PDF, TXT and Markdown.

**Model configuration** — 15 OpenAI-compatible providers preset (8 domestic, 7 international).
Bring your own API key. The model list can be pulled live from each vendor's `/v1/models`.

## Architecture

```
┌──────────────────────── Your PC (Windows) ─────────────────────────┐
│                                                                    │
│  Electron shell — frameless window, in-app title bar               │
│        │  loads http://127.0.0.1:<port>/                           │
│        ▼                                                           │
│  FastAPI backend (PyInstaller sidecar, serves the frontend)        │
│    • LangGraph fixed pipeline, SQLite checkpoints                  │
│    • Market tools: eastmoney-direct → sina → tencent → akshare     │
│    • SQLite: conversations, reports, settings                      │
│        │                                                           │
│        │  HTTPS — the only outbound hop                            │
│        ▼                                                           │
│  Cloud LLM API — your key, your chosen vendor                      │
└────────────────────────────────────────────────────────────────────┘
```

Your questions, your holdings and your generated reports never leave the machine. Market data comes
from public endpoints. The only request that carries your API key goes to the LLM vendor you chose.

## Install

Download from the [**latest release**](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest):

| File | Use |
| --- | --- |
| `JingYuan-Setup-1.2.2.exe` | Installer. Creates Start-menu and desktop shortcuts. |
| `JingYuan-Portable-1.2.2.exe` | Portable. Runs without installing. |

Windows 10/11, x64. No Python or Node runtime required — everything is bundled.

The binaries are unsigned, so SmartScreen will warn on first launch. Choose **More info →
Run anyway** if you trust this source.

## First run

1. Launch JingYuan. The backend starts automatically and the window opens on **专业对话** (Chat).
2. Go to **模型配置** (Providers), pick a vendor, paste your API key, and select a model.
3. Click **设为当前推理模型** (Set as active model).
4. Return to Chat, or go to **研究任务** (Research) and generate your first report.

Without a key, market data, charts and diagnostics still work — only the model-written prose is
replaced by rule-based templates.

## Where your data lives

Everything sits in `%APPDATA%\FinResearch\`:

- `finresearch.db` — conversations, reports, model configuration, user profile
- `backend.log` — backend log
- `port.txt` — the local port the backend bound to

To reset the app completely, close it and delete that folder. To back it up, copy it.

API keys are stored in this local database in plain text. They are transmitted only to the LLM
vendor you selected.

## Disclaimer

JingYuan is a research aid. It does not provide investment advice, does not connect to any broker,
and cannot place orders. Model output may be wrong or incomplete. Market data comes from free public
sources and may be delayed, revised, or unavailable. **Every investment decision, and every
consequence of it, is yours.** Markets carry risk.

## License

Free for **personal, non-commercial use**. Commercial use, redistribution and reverse engineering
are prohibited. Source code is not published. See [LICENSE](LICENSE).

---

<div align="center">
<sub>景渊 JingYuan · Built for research, not for trading.</sub>
</div>
