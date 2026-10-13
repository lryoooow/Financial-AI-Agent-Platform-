<div align="center">

<img src="assets/logo-256.png" width="120" alt="JingYuan">

# JingYuan

[![Download](https://img.shields.io/badge/Download-v2.7.0-blue?style=for-the-badge&logo=github)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)

**English** · [简体中文](README.zh-CN.md)

</div>

---

**JingYuan is a privacy-first AI research assistant for A-share investors.** It runs entirely on your own computer, pulls real market data, writes research reports with zero fabricated numbers, and remembers your conversations — all without uploading a single byte to anyone else's server.

It is a research tool, not a trading tool. No broker integration, no order placement, ever.

---

## What you can do

### 💬 Ask anything

> "What's Moutai trading at?" → Instant answer, timestamped
>
> "Compare Moutai vs Wuliangye" → Real data, side by side
>
> "Is baijiu overvalued?" → Industry PE / PB / dividend yield
>
> "What was I bullish on?" → It searches your past conversations

### 🧠 It remembers

Tell it once — it remembers. Your research reports, uploaded documents, and conversation history become a personal knowledge base it draws from in future chats.

### 📊 Watch the market

Five major indices live on the homepage. Click into any stock for intraday, candles, order book, and indicators.

### 📝 Generate research reports

Pick a stock, pick a template. The agent pulls data, writes a full report streaming live from first word to last. Export to Word / PDF / Markdown.

### 📂 Build your knowledge base

Upload your own research (PDF / Word / Markdown / TXT). The agent searches it alongside live data.

---

## Quick start

1. Download and install
2. Go to **Model Configuration** → pick a provider → paste your API key
3. Start asking

**Windows 10/11 x64** · No Python/Node needed · SmartScreen → "Run anyway"

---

## Why local matters

Your data — conversations, documents, reports, API keys — never leaves `%APPDATA%\FinResearch\`.

No cloud. No accounts. No tracking. The only outbound request goes to your chosen LLM provider.

---

## Three rules, enforced in code

| Rule | Meaning |
|------|---------|
| **Zero fabricated numbers** | Real APIs only. Missing data = "—", never 0.0 |
| **Traceable** | Sources cited. AI vs template paragraphs labeled |
| **Advisory only** | No buy/sell directives. Disclaimers enforced |

---

## Tech stack

Electron · FastAPI · LangChain / LangGraph · deepagents · SQLite · sqlite-vec

[CHANGELOG](CHANGELOG.md) · [Data layout](backend/DATA_LAYOUT.md)

---

<div align="center">

*JingYuan — look far, look deep.*

*A research tool, not investment advice.*

</div>
