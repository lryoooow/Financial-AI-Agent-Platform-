# FinResearch · 家用本地金融研究助手后端

给资深个人投资者（父母）用的本地辅助工具。**不是企业 SaaS**。
技术栈：FastAPI(async) + deepagents 智能体（LangChain/LangGraph）+ SQLAlchemy2 async + akshare + pandas-ta-classic + Jinja2。

> 三铁律：① 数字零幻觉（数值全由工具库算，LLM 只引用）② 可溯源（每个返回带 `provenance`）③ 含免责（报告带"AI 生成、仅供参考、非投资建议"）。

## 当前版本

**2.0.4** — 让整篇连续撰写真正落到界面：报告区不再预摆空章节骨架，段落切分改为宽容匹配。完整更新历史见 [CHANGELOG](../CHANGELOG.md)。

- **2.0.4**：报告区去掉预渲染的空章节骨架与逐章转圈，标题随正文一起出现；段落切分容忍
  `##`/`**`/序号/裸标题等写法（此前只认【】，模型换写法就整篇判空、静默降级回分段）。
- **2.0.3**：报告从串行分段改为整篇一次撰写（段间无停顿）；删除写死的字数/字符/token 限制，
  篇幅由 low/medium/high/max 档位决定；报告正文补上禁 Markdown 规范并统一清洗（emoji 保留）；
  修正 `max` 档下发非法推理参数导致 400 的问题。
- **2.0.2**：对话智能体新增当日分时工具（此前问分时会答"没有分时接口"再用日线凑答案）；
  分时补齐昨收与官方均价，涨跌幅/振幅可用；提示词新增能力边界纪律。
- **2.0.1**：行情页与研究页互斥渲染，移除混排兼容逻辑，并增加 Electron 页面缓存失效策略。
- **2.0.0**：新增独立行情分析页、1 分钟分时和增强 K 线/技术指标；支持图片资产与行情上下文流式问答；
  修复非默认股票、北交所 `920` 代码、切股串标和数据源降级问题；推理强度支持 `low/medium/high/max`。
- **1.5.0**：研究报告分段**真流式**（逐段串行、段内逐 token，非前端假动画）；报告库支持**真删除**
  （物理删报告 + 来源任务 + 检查点，不可恢复）；修复对话偶发 500（SQLite 写锁竞争，加
  `busy_timeout`+WAL，实测多轮零 500）。
- **1.4.1**：修复模型偶发空返回被当成空白章节的问题（表现为"多数章节空白、有些有有些没有"）。
- **1.4.0**：后端从固定 LangGraph 管线整体迁移到 deepagents 智能体；对话可自主调工具、跨标的对比；
  新增开放式行业研究；研究进度改为真实工具调用轨迹。

## 启动

```bash
# 1) 起 Postgres + pgvector
docker compose up -d postgres

# 2) 装依赖（建议 Python 3.12；3.14 部分 wheel 滞后）
pip install -e .

# 3) 配环境
cp .env.example .env
#   填 LLM_API_KEY（OpenAI 兼容；前端 providers.ts 里的 baseUrl 都可填）
#   不填则实时端点照常工作，/api/research 与 /api/chart/analyze 会优雅降级

# 4) 跑
uvicorn finresearch.main:app --reload --port 8000
#   打开 http://localhost:8000/docs
```

> akshare 免费、免 key、A 股优先。美股接口多为 v2，本后端默认面向 A 股。

## Windows 注意（父母家用机大概率是 Windows）

- **psycopg 需 pq 后端**：`pip install -e .` 会装 `psycopg[binary]`（自带 libpq），无需另装。
  若仍报 `no pq wrapper available`，手动 `pip install psycopg-binary`。
- **事件循环**：psycopg/SQLAlchemy async 与 Windows 默认 ProactorEventLoop 冲突，
  `main.py` 顶部已自动切到 `WindowsSelectorEventLoopPolicy`，无需手动处理。
- **防挂死**：`main.py` 设了 `socket.setdefaulttimeout(15)`——外部数据源（akshare/东财）
  偶发反爬/不可达时请求 15s 内失败返回（带 `provenance.error`），不会挂死服务。

## 前后端联调（跑通整个软件）

```bash
# A) 后端（本目录）
docker compose up -d postgres     # 起 PG（可选；不起则检查点降级 MemorySaver）
pip install -e .
cp .env.example .env              # 填 LLM_API_KEY（OpenAI 兼容端点）
uvicorn finresearch.main:app --reload --port 8000

# B) 前端（sibling 目录 fronted/）
cd ../fronted
pnpm install && pnpm dev          # Vite 默认 http://localhost:5173
```

- 前端默认连 `http://localhost:8000`；要改在 `fronted/.env` 设 `VITE_API_BASE=...`。
- **优雅降级**：后端没起时，前端行情页回退本地 mock、研究工作台回退 mock 演示、
  图表 AI 速读静默不显示——UI 不会坏。后端起 + akshare 通 → 真实 A 股数据。
- 契约两端对齐见 `fronted/src/app/api/client.ts` 与本文档"端点"表。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{status, db, llm}` |
| GET | `/api/resolve?q=&limit=` | 模糊匹配 A 股标的 |
| GET | `/api/quote/{code}` | 实时快照（Stock 形状） |
| GET | `/api/quote/{code}/intraday` | 分时 `[{time,price,avg,volume}]` |
| GET | `/api/quote/{code}/kline?n=60&adjust=qfq` | 日 K + MA `[{date:M/D,...,ma5,ma10,ma20}]` |
| GET | `/api/quote/{code}/book` | 五档盘口 `{asks,bids}` |
| GET | `/api/quote/{code}/indicators` | `[{name,value,signal}]`（MA/MACD/RSI/KDJ/BOLL） |
| POST | `/api/research` | `{code,template,query?}` → `{task_id}` |
| GET | `/api/research/{id}/stream` | SSE：`node/tool/token/done/error` |
| GET | `/api/reports/{id}` | ReportDoc（含 html） |
| POST | `/api/chart/analyze` | 轻量图表速读（单次 LLM） |

`code` 接受 `600519.SH` 或 `600519` 两种格式（内部归一化）。

## Agent 编排（1.4.0：从固定管线换成 deepagents）

对话与研究都由 deepagents 智能体驱动，模型自主决定调哪些工具（不再是写死的节点顺序）。
早期版本是 `START → entity → research → market → report → END` 的固定 LangGraph 管线，已移除；
那五个 worker 仍是报告质量的骨架，现由研究链路的工具直接调用，取数/撰写逻辑一行未改。

- **对话**（`agents/deep/chat_agent.py`）：agent 按需调工具（行情/财务/新闻/技术指标/行业估值），
  能跨标的对比、多步追问。上下文治理交给 deepagents 的 Summarization + Filesystem 中间件。
- **研究**（`agents/deep/research_agent.py`）：
  - 个股 → `prefetch_basics`（并行取行情/财务/新闻/K线）→ `compose_report`
  - 行业 → `prefetch_industry`（申万行业估值 + 资讯）→ `compose_report`
  - 大块数据（120 根 K 线等）不进模型上下文，只回紧凑摘要；数值由报告各段的事实块精确投喂，
    模型结构上无从伪造。
- **报告**：`report_node` 组装 ReportDoc + Jinja2 渲染（渲染前 pydantic 校验 as_of/sources/risks/
  disclaimer 非空）+ 分段并发撰写（数据不足的段如实标注"不展开"，单段独立降级）。

数字纪律：所有数值由本地工具（akshare/pandas-ta-classic/申万接口）计算，agent 不挂任何计算或
shell 工具，只能引用工具返回值，取数失败如实说明，绝不编造。

检查点（对话与研究共用）：`AsyncSqliteSaver`（桌面默认，thread_id=会话/任务 id）/
`AsyncPostgresSaver`（服务端）；均不可用时降级 `MemorySaver`（日志告警）。
agent loop 上限 `agent_recursion_limit=30`（langgraph super-step，实测覆盖多标的对比+财务追问）。

## 报告模板

`src/finresearch/report_templates/`：`base.html` 含 `{% block header/summary/financials/valuation/risks/sources/disclaimer %}`，主题色变量（红涨绿跌），中文友好字体。`equity_deep/industry/earnings/risk.html` extends base。渲染前强制校验四字段非空，缺则抛错。

## 已知限制

- akshare 为东财/新浪抓取，偶有反爬/字段名变化；实时端点失败会返回 `provenance.error`，前端可据此显示降级。
- **个股 PE/PB/总市值/行业 可能显示"未获取"**：这几项只有东方财富接口提供，而东财的 push2 端点
  在部分网络环境下不可达（直连 RemoteDisconnected，走代理则代理常断）。行情价格走 sina（稳定），
  但 sina 不返回这些估值字段。报告会如实标注"未获取"而非编造。行业级 PE/PB 走申万/新浪接口，正常可取。
  若需恢复个股估值：排查本机到 `push2.eastmoney.com` 的网络，或接入第三方源（`TUSHARE_TOKEN` 已留槽位）。
- 检查点降级 MemorySaver 后任务状态不持久（重启丢失）。
- PDF 导出需 `pip install -e ".[pdf]"` 装 weasyprint（Windows 需 GTK）。
- 财务指标接口字段名以 akshare 2026 现状为准，缺失字段显示"—"而非 0。
