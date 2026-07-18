# 金融研究助手 — 家用本地版规划（v1）

> 给父母（资深个人投资者）用的**本地辅助研究工具**。本地桌面应用 + 本地后端 + 云端 LLM API。
> 前端由用户自建，本文档定义**后端与整体架构**。
> 技术栈：LangChain + LangGraph + FastAPI(本地) + PostgreSQL(pgvector)；LLM 走云端 OpenAI 兼容 API。
> 覆盖：A 股优先（v1），美股 v2。

---

## 0. 定位与铁律

**定位**：单用户、单机、本地运行、隐私优先。父母是资深投资者，工具价值在"透明可溯源、把分散数据快速聚合并算清楚"，不是手把手。AI 只做研究辅助，最终判断在人。

**三条铁律**（保留，家用版更轻）：
1. **数字零幻觉** — 所有数值由工具/库算（行情、指标、估值、风险），LLM 只引用、不手算；报告生成后做对账校验。资深投资者最烦"AI 瞎编数字"。
2. **可溯源** — 报告每个数字能点回 `{源接口, 时间, 原始值}`。
3. **免责声明** — 不输出买卖指令性动词；报告带"AI 生成、仅供参考、非投资建议"。家用版不再是牌照防御，而是好习惯 + 提醒父母别盲信。

**已剥离的企业级东西**（家用不需要）：多租户、RBAC 三角色、登录鉴权、审计合规(WORM/SIEM/MiFID)、arq/Redis 队列、云部署、商业化分期、Langfuse 云 tracing。

---

## 1. 系统架构（本地）

```
   父母的电脑 (Windows)
   ┌─────────────────────────────────────────────────┐
   │  桌面前端 (你自建：Flet / Tauri / Electron 任选)   │  走势图 + 报告展示 + 输入
   │            ↓ HTTP / SSE (localhost)              │
   │  本地后端 FastAPI 服务 (我负责)                   │
   │     • LangGraph 编排 (固定管线)                   │
   │     • 工具层 (akshare / pandas-ta / ...)         │
   │     • PostgreSQL (业务数据 + 图检查点 + pgvector) │
   │            ↓ HTTPS                               │
   │  云端 LLM API (你/父母的 key)                     │  唯一推理出网点
   └─────────────────────────────────────────────────┘
```

- **单进程**：FastAPI 服务里直接跑 LangGraph（`async for ev in graph.astream(...)` 流式吐给前端）。无队列、无独立 worker。
- **数据出网**：行情/财报走 akshare 等免费公开市场数据源（公开数据，非敏感）；查询、持仓、生成的报告全留本机。
- **LLM 出网**：唯一需要带 key 的推理调用（建议国内合规端点）。
- **PostgreSQL 本地完整库**：业务表 + LangGraph 检查点(`AsyncPostgresSaver`) + pgvector（为后续 RAG 预留）。docker-compose 一键起。

---

## 2. LangGraph 编排

**v1 固定管线**（确定性，保证一定出报告）：
```
START → 标的消歧(EntityResolver) → 基本面(Research) → 行情技术(Market) → 报告(Report) → END
```
每个节点产出**结构化对象**进共享 state，Report 只从 state 取字段渲染（不从消息流读数字，防"报告抄错"）。

**为什么不用 `langgraph-supervisor` 包**：它和 langchain 1.x 有已知兼容缺口，且家用单层管线不需要动态路由。v1 用 `StateGraph` 手写固定边，稳、能跑、好调试。**动态 Supervisor 路由留作扩展点**（v3 想做"发现高风险自动追加尽调"再加 LLM 路由）。

**共享状态**（精简，无多租户字段）：
```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

def merge_findings(left: list[dict], right: list[dict]) -> list[dict]:
    seen = {f.get('id') for f in left}
    return left + [f for f in right if f.get('id') not in seen]

class FinanceGraphState(TypedDict):
    normalized_ticker: dict                          # {symbol, exchange, currency, market, name}
    data_as_of: str                                  # 数据截止时间（报告页眉）
    messages: Annotated[list, add_messages]
    findings: Annotated[list[dict], merge_findings]  # 各 Agent 结构化产出（带 provenance）
    indicators: dict                                 # TA 指标（库算）
    risk_flags: list[str]
    draft_report: str | None
    citations: list[dict]
    error: str | None
```

**检查点**：`AsyncPostgresSaver`，`thread_id = task_id`——后端进程重启可从最近一步恢复（父母中途关掉重开能续跑）。开发期可降级 `MemorySaver`。

**流式**：`graph.astream(..., stream_mode=['messages','updates'])` → FastAPI `StreamingResponse(text/event-stream)` 直接推前端。事件：`node`(节点切换)、`tool`(工具进度)、`token`(报告打字机)、`done`、`error`。

---

## 3. 子 Agent（v1 四个）

| 节点 | 职责 | 关键工具 | 产出 |
|---|---|---|---|
| **EntityResolver** | 标的消歧（"茅台"→600519.SH）；v1 取最佳候选，v2 加歧义反问 | `resolve_entity`(akshare) | `normalized_ticker` |
| **Research** | 三表 + 关键指标(ROE/营收增速/资产负债率) + 公司简介 + 新闻 | `get_financial_indicators`、`get_company_profile`、`search_news` | `findings` |
| **Market** | 行情 + 技术指标（MA/MACD/RSI/KDJ/Bollinger…） | `get_kline`、`compute_ta`、`get_realtime_quote` | `indicators` |
| **Report** | 从 state 渲染结构化报告（Jinja2） | `render_report` | `draft_report` |

> v1 不设独立 Risk/News Agent——风险提示内嵌报告模板，新闻并入 Research。v2 再拆。

**Report 硬约束**：强制四字段(`as_of`/`sources`/`risks`/`disclaimer`)非空才渲染；数字只从 state 取（生成后对账）；禁买卖指令性动词。

---

## 4. 数据栈

**v1 全免费源（零 key 也能跑通）**：akshare 主（A股 行情/三表/指标/新闻/宏观/分时）。tushare 作可选质量校验源（个人 token 免费）。

**美股 v2**：FMP（需 key）。akshare 美股接口弱，不进 v1。

**实时走势图**（你问的）：分时图/分钟K/五档免费可得——
- `ak.stock_zh_a_spot_em()` 实时快照、`ak.stock_bid_ask_em()` 五档、`ak.stock_zh_a_minute(period='1')` 分时、`ak.stock_intraday_em()` 分时逐笔。
- 想更低延时：mootdx（通达信协议，免 key）。
- **拿不到**：Level-2（十档/逐笔/毫秒 tick）需付费授权。家用盯单只股分时图，免费源完全够。

**缓存**：三表/指标按 `(symbol, report_period)` 落 PG upsert（不重复拉）；行情快照 TTL 1-5 分钟；分时当日；新闻 5-15 分钟。限流 + 指数退避重试。

**provenance**：每个数字带 `{source, endpoint, fetched_at, raw_value}`；`data_as_of` 贯穿全链路并在报告页眉展示。处理停牌/ST/前复权(qfq)口径。

---

## 5. 前端调用的接口

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/research` | 提交问题 → 返回 `task_id` |
| GET | `/api/research/{id}/stream` | SSE 流：节点进度 + 报告打字机 |
| GET | `/api/reports/{id}` | 取生成的报告（HTML/MD） |
| GET | `/api/resolve?q=` | 标的搜索联想（输入框用） |
| GET | `/api/quote/{code}` | **实时快照**（现价/涨跌/五档），可 SSE 推 |
| GET | `/api/quote/{code}/intraday` | **分时数据序列**，前端画分时图 |
| GET | `/api/health` | 健康检查 |

走势图：前端轮询 `/api/quote/{code}`（每 3-5 秒）或连 SSE 推送；分时图调 `/api/quote/{code}/intraday` 拿序列喂 ECharts/lightweight-charts。

---

## 6. 可观测 / 安全（家用轻量）

- **日志**：本地结构化日志（文件），记录每次任务的工具调用/耗时/错误。可选自托管 Langfuse（本地，非云）看 trace。
- **隐私**：查询/持仓/报告全本地；LLM 调用带自家 key（建议国内合规端点：通义/智谱/DeepSeek）。
- **只读工具**：永不接交易/下单/转账。Agent 只能读、不能碰钱。
- **免责**：报告带"AI 生成、仅供参考"。
- **无登录/多租户**：单用户本地工具，不需要。

---

## 7. 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph（StateGraph 固定管线；v1 不用 langgraph-supervisor 包）|
| LLM | OpenAI 兼容 API（云端，自家 key）|
| 后端 | FastAPI(async，本地单进程) |
| 数据库 | PostgreSQL 16+（业务表 + AsyncPostgresSaver 检查点 + pgvector 预留）|
| 驱动 | psycopg（SQLAlchemy async 与 LangGraph 检查点共用，不引 asyncpg）|
| 技术分析 | pandas-ta-classic（纯 Python 免编译，纯 pandas 兜底）|
| 数据源 | akshare 主（A股）、mootdx（低延时备选）、tushare（可选校验）、FMP（美股 v2）|
| 联网搜索 | akshare `stock_news_em`（免费默认）+ Serper/Tavily（付费可选）|
| 报告 | Jinja2 + WeasyPrint（PDF 可选开关，默认 HTML）|
| NLP | LLM `with_structured_output`（不跑本地 FinBERT/UIE，省 GPU）|
| 引用校验 | rapidfuzz（轻量纯 Python）|

---

## 8. 目录结构（仅后端；前端你自建，放 sibling 目录）

```
finresearch/
├── backend/                      # 我负责
│   ├── pyproject.toml
│   ├── .env.example              # LLM key / 数据源 key / PG 连接
│   ├── docker-compose.yml        # postgres + pgvector
│   ├── src/finresearch/
│   │   ├── main.py               # FastAPI app + CORS + 启动建表
│   │   ├── config.py
│   │   ├── api/routes.py         # 上面 7 个端点 + SSE
│   │   ├── db/{session,models}.py
│   │   ├── agents/{state,graph,workers/*}.py
│   │   ├── tools/{entity,market,fundamentals,news,report}.py
│   │   └── report_templates/{base,equity_deep}.html
│   └── README.md
└── frontend/                     # 你负责（任意技术栈）
```

---

## 9. 开发路线（家用版分期）

**v1（现在）— A 股闭环**
- 固定管线四节点 + 工具层（akshare 免费源）+ PG + 流式报告 + **实时分时图端点**
- 能跑通：输入 A 股标的 → 拉行情/财务/新闻 → 出 HTML 报告（带来源+免责）→ 前端流式看 + 画分时图

**v2 — 美股 + 体验**
- 美股（FMP）、报告内嵌图表（MA/MACD/RSI）、PDF 导出、标的消歧反问、更多报告模板（行业/财报点评）

**v3 — 知识库 + 加固**
- 本地 RAG（pgvector，公告/研报检索）、引用锚定校验加固、可选动态 supervisor、可选人审

**永不做**：交易/下单、多用户/多租户、云部署商业化。

---

## 10. 关键决策

1. **本地桌面工具**，不是云 SaaS。隐私优先、零运维。
2. **固定管线 MVP**（StateGraph 手写），不用 langgraph-supervisor 包（避版本坑），动态路由留扩展点。
3. **单进程** FastAPI 跑 LangGraph + SSE 流式，无 arq/Redis。
4. **PostgreSQL 完整本地库**（业务 + 检查点 + pgvector 预留），psycopg 单驱动。
5. **LLM 云端 API + 自家 key**，不跑本地大模型（保中文金融分析质量）。
6. **NLP 用 LLM structured output**，不跑本地 FinBERT/UIE/torch（家用电脑省 GPU、省安装）。
7. **v1 只 A 股 + 免费源**（akshare），零 key 也能跑通。
8. **走势图**：后端给数据序列，前端画图。
9. **只读工具铁律**：永不接交易。

---

## 11. 待定

1. LLM 用哪家国内兼容端点 + 哪个模型？（建议先 benchmark 工具调用准确率 + 中文金融理解）
2. 桌面前端你打算用哪个栈？（影响后端 SSE/接口细节，但你自建我不干涉）
3. Python 版本：3.12 起步（生态稳）还是坚持 3.14（承担部分库 wheel 滞后）？
4. 报告 v1 出 HTML 就行，PDF 作为可选开关？（WeasyPrint 在 Windows 装 pango 略麻烦）
