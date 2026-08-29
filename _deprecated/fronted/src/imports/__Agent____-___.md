# 金融研究助手 — 工具层详细规划（家用本地版 v1）

> 配套文档：`金融Agent平台规划.md`（家用本地版架构）。本文聚焦**工具层**——逐工具落到「用哪个开源库、Python 签名、输入输出、缓存、溯源」。
> 场景：父母家用、本地单机、A 股优先（v1）、LLM 走云端 API。
> 铁律：**数字零幻觉**（数值由库算，LLM 只读 JSON）、**可溯源**（每工具返回带 `provenance`，联网事实带引用锚定）、**免责声明**。

---

## 0. 推荐开源栈（一句话）

> **AKShare（数据，含分时/五档）+ pandas-ta-classic（技术分析）+ empyrical-reloaded（风险）+ FinanceToolkit（美股比率，v2）+ Serper/Tavily（联网搜索，可选）/ akshare 免费新闻（默认）+ trafilatura（正文抽取）+ LLM `with_structured_output`（NER/情感/事实抽取，免本地模型）+ Jinja2（报告，PDF 可选）+ rapidfuzz（引用锚定）** —— 全 MIT/Apache、纯 Python 免编译、家用电脑零 GPU、所有数值库算 LLM 只读。

---

## 1. 联网搜索

### 1.1 双阵营（按用途分工）

| 阵营 | 代表 | 特点 | 适合 |
|---|---|---|---|
| **A. SERP 代理** | Serper / SerpAPI / Google CSE | 代理 Google SERP，返回 JSON(title/snippet/url)，新鲜度高，但是**摘要非正文**，需自己做正文抽取 | 实时财经新闻/公告 |
| **B. AI 原生搜索** | Tavily / Exa / You.com | 为 LLM 设计，直接返回干净 markdown，开箱喂 LLM，单价更高 | 深度研究/研报 |

**家用分工**：
- **实时新闻**：v1 直接用 **akshare `stock_news_em`**（免费、中文、免 key）；想更全上 **Serper**（$0.30-1/1k 最便宜，中文源覆盖全）。
- **深度研究**（行业/研报）：**Tavily Research mode + Exa**（语义搜索）。家用可选用，v1 可暂不上。

### 1.2 避坑

- **Bing Web Search API** 已 2025-08 停用，旧 `langchain BingSearchAPIWrapper` 全失效——别基于 Bing 写代码。
- **DuckDuckGo(ddgs)** 免费但限流不稳（频繁被封）——仅零成本调试兜底。
- **Brave Search API** 默认 TOS 禁 AI 推理——不推荐。

### 1.3 正文抽取（SERP 结果必经）

- 主选 **trafilatura**（本地，原生 markdown，F1 最高，零 API 成本，3.14 wheel 齐全）。
- 轻量备选 **readability-lxml**；JS 重站兜底 **Jina Reader**（`r.jina.ai/{url}`）。

### 1.4 缓存与引用锚定

- 缓存：静态数据 T+1 落 PG；新闻 5-15 分钟；行情 1-5 分钟；搜索结果按 query hash（新闻类 TTL=5min，研究类 TTL=24h）。
- 每条结果带 `{url, title, publisher, publish_date, fetched_at, content_md, source_tool}`；下游事实抽取嵌套 `Citation{source_url, source_quote, char_span}` + 引用锚定校验（见 §4）。

---

## 2. 分析库选型（四类各一库）

| 类别 | 主选库 | License | 关键理由 |
|---|---|---|---|
| **技术分析 TA** | **pandas-ta-classic** | MIT | 纯 Python+pandas，224+ 指标全覆盖（MA/EMA/MACD/RSI/KDJ/Bollinger/ATR/OBV/Williams/Ichimoku），零 C 编译，3.14 wheel 齐全。是已删库的 pandas-ta 的继任者。 |
| **基本面/比率（A股）** | **AKShare + Tushare(可选校验)** | MIT / 个人免费 | AKShare 三表 + 70+ 指标(`stock_financial_analysis_indicator` 直接给 ROE/资产负债率/营收增速)；Tushare `fina_indicator` 字段更规范，作可选校验。 |
| **基本面/比率（美股 v2）** | **FinanceToolkit** | MIT | 200+ 比率 + DuPont + WACC，公式透明可审计。v2 美股时用。 |
| **估值（DCF/DDM）** | **自写**（numpy-financial + FinanceToolkit WACC） | BSD | 无权威成品库，纯函数管道每条假设显式化，适配 provenance。 |
| **风险/组合** | **empyrical-reloaded**（+ PyPortfolioOpt v3）| Apache-2.0 / MIT | alpha/beta/最大回撤/波动率/VaR/CVaR 权威实现，纯函数式，天然适合 `@tool`。 |

**坚决避开**：原版 pandas-ta（删库/供应链风险）、finta（archived）、tulipy（unmaintained）、vectorbt（numba 3.14 坑）。

---

## 3. 工具矩阵（按 Agent 分组）

> 所有工具：**数值由库计算，LLM 只读 JSON**；返回结构化 dict 带 `provenance`；走统一缓存+限流+重试。

### 3.0 EntityResolver（守门节点）

| 工具 | 职责 | 库 | 签名 |
|---|---|---|---|
| `resolve_entity` | 模糊名称(简称/拼音) → 统一主键(ts_code)，覆盖别名/退市 | AKShare(`stock_info_a_code_name`) | `def resolve_entity(query: str) -> dict` |
| `validate_code` | 校验代码有效/退市/暂停 | AKShare(`stock_zh_a_spot_em`) | `def validate_code(ts_code: str) -> dict` |

输出示例：`{"ts_code":"600519.SH","name":"贵州茅台","market":"CN","confidence":0.98,"provenance":{...}}`。缓存 30 天。

### 3.1 Research Agent

| 工具 | 职责 | 库 | 缓存 |
|---|---|---|---|
| `get_company_profile` | 主营/行业/上市/董监高 | AKShare(`stock_individual_info_em`/`stock_zyjs_em`) | 30 天 |
| `get_financial_indicators` | 三表 + ROE/营收增速/资产负债率/毛利率（**直接取，绝不自己算**）| AKShare(`stock_financial_analysis_indicator`) | 按报告期永久 |
| `search_news` | 财经新闻（v1 免费默认） | AKShare(`stock_news_em`)；Serper/Tavily 可选 | 5-15 分钟 |

```python
def get_financial_indicators(ts_code: str, years: int = 5) -> list[dict]:
    """返回 [{report_period, roe, roa, gross_margin, net_margin, debt_ratio,
    revenue_yoy, provenance:{source, fetched_at}}]"""
```

### 3.2 Market Agent（行情 + 技术 + **实时走势图**）

| 工具 | 职责 | 库 | 缓存 |
|---|---|---|---|
| `get_kline` | 历史日/周/月 K（前复权），画 K 线 + 算指标 | AKShare(`stock_zh_a_hist`) | T+1 落库 |
| `compute_ta` | **技术指标（LLM 绝不算）** | pandas-ta-classic + 纯 pandas 兜底 | 随 K 线 |
| `get_realtime_quote` | 实时快照：现价/开高低收/涨跌幅/量额/市值 | AKShare(`stock_zh_a_spot_em`) / mootdx（低延时）| 1-5 秒 |
| `get_bid_ask` | 五档盘口 | AKShare(`stock_bid_ask_em`) / mootdx | 1-3 秒 |
| `get_intraday` | **分时数据序列**（1 分钟 K / 分时逐笔），前端画分时图 | AKShare(`stock_zh_a_minute(period='1')`/`stock_intraday_em`) | 当日 |

```python
def compute_ta(ohlcv: pd.DataFrame, spec: dict) -> dict:
    """spec = {"ema_20":{"kind":"ema","length":20}, "macd":{...}, "rsi_14":{...}}
    返回 {ema20, macd, macd_signal, macd_hist, rsi14, bb_upper/lower/mid,
    atr14, stoch_k/d/j, provenance:{lib, version, computed_at}}"""

def get_intraday(ts_code: str, period: str = "1") -> list[dict]:
    """返回 [{time, price, avg_price, volume}, ...]，前端直接喂 ECharts 画分时图。
    period: '1'/'5'/'15'/'30'/'60' 分钟。仅当日有效。"""
```

**走势图说明**：后端只给**数据序列**，前端画图（ECharts / lightweight-charts / TradingView）。分时图 = `get_intraday`；K 线 = `get_kline`；实时现价 = `get_realtime_quote`（前端轮询或 SSE 推）。**Level-2（十档/逐笔/毫秒）免费拿不到，需付费授权**——家用免费源够。

### 3.3 Report Agent

| 工具 | 职责 | 库 |
|---|---|---|
| `validate_report_context` | pydantic 校验，强制 4 字段(`as_of`/`sources`/`risks`/`disclaimer`)非空 | pydantic v2 |
| `render_report` | 结构化 dict → HTML（PDF 可选开关） | Jinja2 + WeasyPrint(可选) |

### 3.4 联网分析（v2/v3，v1 用 LLM 直抽）

家用电脑不跑本地 NER/情感模型（PaddleNLP UIE / FinBERT 要 GPU 且 3.14 安装麻烦）。**v1/v2 直接用 LLM `with_structured_output` 一步抽实体+情感+事实**，省一整条本地模型链。引用锚定校验（rapidfuzz，纯 Python 轻量）保留。

| 工具 | 职责 | 库 |
|---|---|---|
| `extract_article` | URL → 干净 markdown（trafilatura→readability→Jina）| trafilatura + readability-lxml + Jina |
| `extract_financial_facts` | 结构化事实抽取（含嵌套 Citation） | LangChain `with_structured_output` + pydantic |
| `verify_citation` | **引用锚定校验（防伪造核心）** | rapidfuzz |

---

## 4. 联网结果 → 金融分析管线（v2 启用；v1 可选）

```
URL → ①trafilatura 抽取清洗 → ②段落切分(保留偏移)
    → ③LLM with_structured_output 抽 FinancialFact(实体+事件+情感+数值，嵌套 Citation)
    → ④引用锚定校验(精确子串 → rapidfuzz≥0.85 → 失败丢弃/降级)  ← 防伪造核心
    → ⑤带脚注 [n] 落报告 + JSONL 复用
```

**Schema**（字段名语义化引导 LLM 填充，method 优先 `tool_calling`）：
```python
from pydantic import BaseModel
from typing import Literal

class Citation(BaseModel):
    source_url: str
    source_quote: str          # 必须是原文逐字片段
    char_span: tuple[int, int]

class FinancialFact(BaseModel):
    subject: str
    event: str
    metric: str | None = None
    value: float | None = None
    currency: str | None = None
    period: str | None = None
    sentiment: Literal['positive', 'negative', 'neutral']
    confidence: float
    citation: Citation         # 嵌套引用，必填
```

**引用锚定校验（不可省）**：
```python
from rapidfuzz import fuzz

def verify_citation(source_quote: str, cleaned_text: str, threshold: float = 0.85) -> dict:
    if source_quote in cleaned_text:                         # 精确子串优先
        i = cleaned_text.index(source_quote)
        return {"verified": True, "match_type": "exact", "score": 1.0,
                "char_span": [i, i + len(source_quote)]}
    best, best_idx = 0.0, -1                                 # 滑窗模糊兜底
    step = max(1, len(source_quote) // 4)
    for i in range(0, max(0, len(cleaned_text) - len(source_quote)), step):
        s = fuzz.ratio(source_quote, cleaned_text[i:i+len(source_quote)])
        if s > best: best, best_idx = s, i
    if best >= threshold:
        return {"verified": True, "match_type": "fuzzy", "score": best,
                "char_span": [best_idx, best_idx + len(source_quote)]}   # 降 confidence
    return {"verified": False, "match_type": "failed", "score": best, "char_span": None}
```
没有这层校验，带引用等同于 LLM 自说自话。家用版目标通过率 >95%。

---

## 5. 报告模板（4 种 + Jinja2 骨架）

### 5.1 强制四字段（渲染前 pydantic 校验，缺一不渲染）
```python
from pydantic import BaseModel, model_validator

class ReportContext(BaseModel):
    as_of_date: str          # 数据时点（强制）
    sources: list            # 引用来源（强制）
    risks: list              # 风险提示（强制）
    disclaimer: str          # 免责声明（强制）
    # ... 其他业务字段
    @model_validator(mode='after')
    def _check(self):
        for f in ('as_of_date', 'sources', 'risks', 'disclaimer'):
            assert getattr(self, f), f"报告强制字段 {f} 缺失，中止渲染"
        return self
```

### 5.2 四种模板

| 模板 | 章节要点 | 关键数据 |
|---|---|---|
| **个股深度** | 投资摘要/公司概况/行业/商业模式/财务分析(3-5年)/盈利预测/估值(可比公司表)/风险 | financials.rows、valuation.comps |
| **行业研究** | 市场规模+CAGR/竞争格局(CR3/CR5)/产业链/政策/受益标的 | market_size、competition |
| **财报点评** | 业绩快评(营收/净利+YoY/QoQ+超预期)/经营拐点/分业务/预测调整 | beat_miss、forecast_revision |
| **风险评估** | 市场风险(beta/VaR/CVaR)/下行风险/流动性/基本面(F-Score)/情景分析 | risk_metrics、scenarios |

### 5.3 个股深度报告 Jinja2 骨架

**base.html**（共享骨架 + CSS）：
```html
<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<style>
  body { font-family: "Microsoft YaHei",sans-serif; font-size: 11px; line-height: 1.6; }
  h1 { font-size: 20px; color: #b00; border-bottom: 3px solid #b00; padding-bottom: 6px; }
  h2 { font-size: 15px; color: #b00; border-left: 5px solid #b00; padding-left: 8px; }
  table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 10px; }
  th,td { border: 1px solid #ccc; padding: 4px 6px; text-align: center; }
  th { background: #f5f0f0; color: #b00; }
  .summary-box { background: #fff8f0; border: 1px solid #e0c0a0; padding: 10px; }
  .disclaimer { font-size: 8px; color:#999; margin-top: 20px; border-top: 1px solid #ccc; padding-top: 8px; }
  .src { font-size: 8px; color:#666; }
</style></head><body>
  {% block header %}{% endblock %}
  {% block investment_summary %}{% endblock %}
  {% block financial_analysis %}{% endblock %}
  {% block valuation %}{% endblock %}
  {% block risks %}{% endblock %}
  {% block sources %}{% endblock %}
  {% block disclaimer %}{% endblock %}
</body></html>
```

**equity_deep.html**（`extends base`）：
```html
{% extends "base.html" %}
{% block header %}
  <h1>{{ company_name }} ({{ ticker }}) · 个股研究</h1>
  <div class="src">报告日期:{{ report_date }} · 数据时点:{{ as_of_date }}</div>
{% endblock %}
{% block investment_summary %}
  <h2>一、摘要</h2>
  <div class="summary-box"><p><b>核心逻辑:</b> {{ core_thesis }}</p><p><b>催化剂:</b> {{ catalysts }}</p></div>
{% endblock %}
{% block financial_analysis %}
  <h2>二、财务分析</h2>
  <table><thead><tr><th>指标</th>{% for y in financials.years %}<th>{{ y }}</th>{% endfor %}</tr></thead>
    <tbody>{% for row in financials.rows %}
      <tr><td style="text-align:left">{{ row.name }}</td>{% for v in row.values %}<td>{{ v }}</td>{% endfor %}</tr>
    {% endfor %}</tbody></table>
{% endblock %}
{% block valuation %}
  <h2>三、估值（可比公司）</h2>
  <table><tr><th>公司</th><th>市值(亿)</th><th>PE</th><th>PB</th><th>营收增速</th></tr>
    {% for c in valuation.comps %}<tr><td>{{ c.name }}</td><td>{{ c.mcap }}</td><td>{{ c.pe }}</td><td>{{ c.pb }}</td><td>{{ c.rev_growth }}</td></tr>{% endfor %}
  </table>
{% endblock %}
{% block risks %}<h2>四、风险提示</h2>{% for r in risks %}<div>· {{ r }}</div>{% endfor %}{% endblock %}
{% block sources %}<h2>五、引用来源</h2><ol class="src">{% for s in sources %}<li>{{ s.title }} {{ s.url }}</li>{% endfor %}</ol>{% endblock %}
{% block disclaimer %}<div class="disclaimer">{{ disclaimer }}</div>{% endblock %}
```

**渲染（HTML 默认，PDF 可选）**：
```python
from jinja2 import Environment, FileSystemLoader
ctx = ReportContext(**agent_output_dict).model_dump()   # 强制 4 字段校验
html = Environment(loader=FileSystemLoader('templates')).get_template('equity_deep.html').render(**ctx)
# HTML 直接返给前端；要 PDF（可选）：from weasyprint import HTML; HTML(string=html).write_pdf(...)
```

### 5.4 报告生成库

| 库 | 角色 | 选型 |
|---|---|---|
| **Jinja2** | 模板渲染（必选） | ✅ 主选。纯 Python，3.14 无痛点。 |
| **WeasyPrint** | HTML/CSS→PDF（可选） | ⚠️ 可选。家用默认出 HTML 给前端渲染即可；要 PDF 时 `conda install -c conda-forge weasyprint` 规避 pango。退路 xhtml2pdf（纯 Python）。 |
| **Plotly** | 图表（v2 嵌报告） | 出静态 PNG/SVG。 |

---

## 6. 安装要点

### 6.1 Python 3.14 痛点分级

> 截至 2026-07，多数库尚未官方声明完整支持 3.14（numpy 2.x C-API + PyO3 问题）。pandas 2.3.3 是首个基本兼容版本。

| 痛点级 | 库 | 方案 |
|---|---|---|
| **零痛点**（v1 主力，直接 pip） | pandas-ta-classic、AKShare、mootdx、trafilatura、readability-lxml、empyrical-reloaded、FinanceToolkit、numpy-financial、Jinja2、pydantic v2、rapidfuzz、Plotly、langchain-core | 先锁 `pandas>=2.3.3 + numpy>=2.2` 再 pip install。 |
| **高痛点**（C 依赖） | **TA-Lib**（v1 不碰，pandas-ta-classic 完全替代）、**WeasyPrint**（pango，PDF 才需要，conda-forge 装） | v1 默认出 HTML，绕开 WeasyPrint。 |

**务实建议**：MVP 用 **Python 3.12**（生态最稳）起步，3.14 作验证目标。家用电脑不必追新版。

### 6.2 个人/家用合规要点（轻量）

家用本地、不分发，合规负担极低，记住几条：
- **akshare/mootdx**：数据版权归交易所/源站，**家用/个人研究 OK**（大家都在用），**别拿去商用分发**。
- **tushare**：个人 token 免费可用；商用要授权（家用不涉及）。
- **yfinance**：个人研究可用，但建议 akshare 为主（美股 v2 用 FMP）。
- **只读铁律**：永不接交易/下单接口。

---

## 7. 待定

1. **LLM 端点/模型**：哪家国内兼容端点 + 哪个模型？建议先小 benchmark（工具调用准确率 + 中文金融理解 + 报告质量）。
2. **A 股指标口径**：ROE 摊薄 vs 加权？资产负债率口径？需在 normalize 层钉死并写进 provenance，否则横向不可比。
3. **实时行情深度**：mootdx 到五档够不够？Level-2（十档/逐笔）家用要不要付费？（建议 v1 五档够用）
4. **报告 PDF**：v1 出 HTML 就行，PDF 作为可选开关？（WeasyPrint Windows 装 pango 略麻烦）
5. **Python 版本**：3.12 起步还是坚持 3.14？
6. **引用锚定 fuzzy 阈值**（rapidfuzz ≥0.85）与降级策略，需实测微调。

---

> **下一步**：你过目 §3.2（走势图工具+端点）、§4（联网分析管线，v2）、§6（安装）。确认后即可据此写 v1 的 `tools/` 骨架代码（每工具一文件 + 单测 mock）。
