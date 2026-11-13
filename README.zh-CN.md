<div align="center">

<img src="assets/logo-256.png" width="112" alt="景渊 JingYuan">

# 景渊 JingYuan

**面向个人投资者的本地优先 · 可自部署 A 股 AI 投研工作台**

[![Official Release](https://img.shields.io/badge/release-v2.0.0%20%E6%AD%A3%E5%BC%8F%E7%89%88-D8B24A)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Web%20%2F%20Docker-0B1220)](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest)
[![Tests](https://img.shields.io/badge/tests-365%20passed-10B981)](backend/tests)
[![License](https://img.shields.io/badge/license-%E4%BB%85%E9%99%90%E4%B8%AA%E4%BA%BA%E9%9D%9E%E5%95%86%E4%B8%9A%E4%BD%BF%E7%94%A8-8B5CF6)](LICENSE)

**简体中文** · [English](README.en.md)

[v2.0.0 正式版说明](#-v200-正式发布版说明) · [界面与功能导览](#️-界面与功能全景导览) · [下载与发行包](#-三种开箱运行形态) · [命令行启动指南](#️-命令行启动与自部署全指南) · [系统架构与安全](#️-系统架构与数据安全)

</div>

---

## 📢 v2.0.0 正式发布版说明

**`v2.0.0` 是景渊（JingYuan）首个架构级焕新的正式发布版本（General Availability）。**

相较早期迭代版本，`v2.0.0` 完成了从桌面单机工具向**全形态专业投研工作台**的跨越：
- **三种开源运行形态一次齐备**：同时提供 **可自部署同源 Web / Docker 版**、**桌面安装版（Windows `.exe` + macOS `.dmg`）** 与 **免安装解压即用版（绿色 `.zip` + 单文件便携版 `.exe`）**。
- **Astryx 风格六大投研模块重构**：覆盖市场总览、看盘与自选（含五维事实体检）、独立深度研究任务台、多轮投研问答（含右侧交互式产物抽屉）、报告与混合知识库（含跨期事实对账单与高清长图分享）、设置与交易日盘后自动巡检推送。
- **十模块解耦智能体引擎**：将底层框架依赖严格收敛于 `engine/` 防腐层，彻底根治多工具并发取数归档断流、代码写法不一致重复报错、孤立 `</think>` 思维链泄漏与重连闪烁问题，全仓通过 **365 项自动化回归测试**。

---

## ⚖️ 三大产品铁律

1. **数字零幻觉** — 所有行情、财务指标、估值倍数与风险统计均由确定性工具获取与计算；`NumberEvidence` 对模型输出与本轮/跨轮工具事实做报告期、量纲（元 / 万 / 亿 / 万亿 / 百分比）及盈亏符号逐项核验，缺失字段显式渲染为 `—`。
2. **逐项可溯源** — 每次取数完整记录数据源、接口与时点（交易日 / 财报披露期）；外部网页、公告与上传文档统一置于 `<untrusted_external_data>` 隔离围栏内，并通过 `verify_citation` 执行字符级引文与数值边界核验。
3. **仅供参考，绝不荐股** — 双轨流式合规守卫同时扫描可见正文与推理思维链（`<think>` / `reasoning_content`），实时拦截买卖建议、仓位指导与目标价表述。

---

## 🖼️ 界面与功能全景导览

### 1. 市场总览（Overview Dashboard）
启动后默认展示顶部实时宽基指数跑马灯、全市场涨跌家数广度条（含涨跌停统计）、沪深市场成交额、申万一级行业涨跌与主力净流入热力矩阵，并在同屏集成自选观察池、快捷发起收盘复盘与最新研报入口。支持快捷键 `Ctrl+K`（macOS 为 `⌘K`）随时唤起全局搜索与指令面板。

![市场总览](assets/screenshots/01-overview.png)

### 2. 看盘与自选 · 五维事实体检（Market Terminal & Watchlist Radar）
- **专业行情终端**：支持当日分时、5 日分时、日/周/月 K 线（前复权 / 后复权 / 不复权）、成交量副图、A 股标准双倍柱 MACD（`2 × (DIF − DEA)`）、Wilder 平滑 RSI(14)、五档买卖盘口，点击任意 K 线或分时点位即可触发按需 **AI 速读**。
- **持久化自选与五维体检**：支持多分组管理、加入时基准价涨跌对比、截图视觉识别与 Excel/CSV/文本核验导入预览，以及基于确定性指标的 **估值 · 盈利 · 量价 · 风险 · 快讯** 五维事实体检雷达。

![看盘与自选](assets/screenshots/02-market-watchlist.png)

### 3. 独立「深度研究」任务台（Deep Research Hub）
独立一级工作区，内置 **个股深度、行业研究、财报点评、风险评估、收盘复盘** 五大专业模板：
- 支持股票代码、中文简称、**拼音首字母实时联想**（如 `gzmt` → `600519.SH 贵州茅台`）及自定义关注问题；
- 采用**单次连续真流式撰写引擎**，按「投资摘要 → 各分析章节」逐 token 实时解析输出，配备 28 秒静默心跳看门狗与 `replay_end` 无闪烁断线原子重放；
- 自动计算并附带 **250 交易日前复权客观风险指标**（年化波动率、沪深300基准 Beta、区间最大回撤、95% 日度 VaR/CVaR）与**证据分层溯源清单**。

![深度研究工作台](assets/screenshots/03-deep-research.png)

### 4. 投研问答与右侧交互式产物抽屉（Research Chat & Artifact Drawer）
- **多轮智能体并发取数**：支持单轮多工具并行调用、多写法证券代码（`688266.SH` / `SH:688266` / `688266`）自动归一复用、同名简称歧义确认（HITL）、粘贴 K 线截图看图解盘与长对话自动压缩（Auto-Compact）。
- **工具胶囊与产物抽屉**：气泡内实时展示工具调用进度胶囊与可折叠思维链；点击消息下方的产物卡片即可在右侧抽屉交互查看完整 K 线、技术指标、250 日风险统计与多期财务报表。

![投研问答与产物抽屉](assets/screenshots/04-chat-artifacts.png)

### 5. 报告与混合知识库 · 跨期事实对账单（Report Ledger & Hybrid RAG）
- **跨期事实对账单（Report Ledger）**：打开任意历史研报即可一键切换「跨期事实对账」，自动拉取最新行情与财报，直观对比研报生成时点 vs 当前现价涨跌、相对沪深300超额收益、期间最大回撤及 PE/PB 估值变化。
- **多格式导出与高清长图**：支持一键导出 **Word (`.docx`) / PDF / HTML / Markdown / TXT** 及自适应排版的**微信高清分享长图**。
- **本地混合知识库（Hybrid RAG）**：支持上传 PDF / DOCX / Markdown / TXT 文档（经无网受限子进程安全解析），基于本地 SQLite FTS5 全文检索 + 向量 RRF 混合重排（可选挂载 PostgreSQL `pgvector`），供跨会话问答随时引用并标注报告日期。

![报告对账与知识库](assets/screenshots/05-reports-ledger.png)

### 6. 设置中心与交易日盘后巡检推送（Settings & Post-Close Scheduler）
- **通用 OpenAI 兼容模型网关**：内置 DeepSeek、通义千问、OpenAI、Gemini、GLM、自定义中转站（one-api / new-api）等供应商配置，支持连通性测试、模型拉取与**思考深度档位（默认 / 低 / 中 / 高）**精细调节。未配置模型时，实时行情、自选体检、知识库检索与规则研报仍可离线正常使用。
- **盘后自动巡检推送**：支持在每个 A 股交易日收盘后（`15:00–23:59`）自动执行收盘复盘与自选股体检，并将带数据来源与合规声明的摘要推送至 **企业微信、飞书、钉钉 Webhook 或 SMTP 邮箱**。

![设置与收盘巡检](assets/screenshots/06-settings-scheduler.png)

---

## 📦 三种开箱运行形态

直接前往 [**GitHub Releases (v2.0.0)**](https://github.com/lryoooow/Financial-AI-Agent-Platform-/releases/latest) 下载预构建好的发行包，所有桌面包均已内置独立 Python 3.13 投研引擎与前端界面，**无需在本机安装 Python 或 Node.js**：

### 形态一：免安装解压即用版（绿色压缩包 / 单文件便携版）

| 平台与形态 | 发行文件名 | 运行与数据存储说明 |
| :--- | :--- | :--- |
| **Windows 绿色压缩包** | `JingYuan-2.0.0-win-x64-portable.zip` | 解压到任意目录，双击 `JingYuan.exe` 直接运行；内置 `.portable` 标记，数据独立保存在同级 `portable-data/` 目录 |
| **Windows 单文件便携版** | `JingYuan-Portable-2.0.0.exe` | 无需解压，双击直接启动，适合随身携带 |
| **macOS 免安装压缩包** | `JingYuan-2.0.0-mac-arm64-portable.zip`<br>`JingYuan-2.0.0-mac-x64-portable.zip` | 分别适配 Apple Silicon（M 系列芯片）与 Intel 芯片，解压后直接运行 |

### 形态二：桌面标准安装版（Windows + macOS）

| 平台与形态 | 发行文件名 | 默认数据目录 |
| :--- | :--- | :--- |
| **Windows 安装包** | `JingYuan-Setup-2.0.0.exe` | `%APPDATA%\FinResearch`（覆盖升级自动保留历史对话、自选与研报） |
| **macOS 安装镜像** | `JingYuan-2.0.0-mac-arm64.dmg`<br>`JingYuan-2.0.0-mac-x64.dmg` | `~/Library/Application Support/FinResearch` |

> **系统首次运行提示**：由于个人开源项目暂未购买商业代码签名证书，Windows 首次打开若出现 SmartScreen 提示，点击 **「更多信息」→「仍要运行」** 即可；macOS 若提示无法验证开发者，可在终端执行 `xattr -cr /Applications/JingYuan.app` 后正常打开。

### 形态三：自部署同源网页版（Docker / 原生命令行）
支持部署在个人电脑、家庭 NAS 或云服务器，通过浏览器访问完整工作台（详见下方命令行指南）。

---

## ⌨️ 命令行启动与自部署全指南

无论你想用 Docker 快速自部署 Web 版、用脚本直接本机启动 Web 服务，还是以开发者模式前后端热重载调试，均可按以下命令行流程操作：

### 1. Docker Compose 容器化部署（推荐服务器 / NAS）

```bash
# 1. 克隆仓库并进入目录
git clone https://github.com/lryoooow/Financial-AI-Agent-Platform-.git
cd Financial-AI-Agent-Platform-

# 2. 复制环境变量模板并按需修改
cp .env.example .env
# 提示：若需在局域网或公网通过反向代理访问，请在 .env 中设置高强度口令 FIN_ACCESS_TOKEN

# 3. 构建并后台启动容器
docker compose up -d --build

# 4. 查看运行状态与实时日志
docker compose ps
docker compose logs -f jingyuan
```

启动完成后浏览器访问 `http://127.0.0.1:9527`，所有持久化数据保存在命名卷 `jingyuan-data` 中。  
也可直接拉取 GitHub Container Registry 预构建镜像运行：

```bash
docker run -d --name jingyuan \
  -p 127.0.0.1:9527:9527 \
  -v jingyuan-data:/data \
  -e FIN_ACCESS_TOKEN="replace-with-strong-secret" \
  ghcr.io/lryoooow/jingyuan-web:v2.0.0
```

### 2. 原生命令行一键启动同源 Web 版（无需 Docker）

前置环境：**Node.js 22+**、**pnpm 10+**、[**uv**](https://docs.astral.sh/uv/)（自动拉取并锁定 Python 3.13 依赖）。

#### Windows（PowerShell）：
```powershell
# 默认构建前端并启动同源 Web 服务（监听 http://127.0.0.1:9527）
pwsh -File scripts/start_web.ps1

# 自定义端口与监听地址示例：
pwsh -File scripts/start_web.ps1 -Port 9528 -BindAddress 127.0.0.1
```

#### macOS / Linux（Bash / POSIX sh）：
```bash
# 默认构建前端并启动同源 Web 服务（监听 http://127.0.0.1:9527）
sh scripts/start_web.sh

# 指定端口与访问口令启动示例：
FIN_ACCESS_TOKEN="your-strong-token" PORT=9528 sh scripts/start_web.sh
```

> **快速重启后端技巧**：若前端 `fronted/dist` 已经构建完成，无需每次重跑前端构建，可直接执行：
> ```bash
> uv run --project backend --locked --no-dev --extra server python backend/run_web.py
> ```

### 3. 前后端分离热重载开发模式（二次开发与调试）

需要分别在两个终端启动 FastAPI 后端（端口 `8000`）与 Vite 前端开发服（端口 `5173`）：

#### 终端一：启动后端开发服务
```powershell
# 首次运行：同步 Python 3.13 开发环境与锁定依赖
uv sync --project backend --locked --extra dev --python 3.13

# Windows PowerShell 启动热重载后端（开启 CORS 开发白名单与 /docs 接口文档）
$env:FIN_DEV="1"; $env:FIN_DOCS="1"
uv run --project backend --locked uvicorn finresearch.main:app --reload --host 127.0.0.1 --port 8000
```
```bash
# macOS / Linux Bash 启动热重载后端
FIN_DEV=1 FIN_DOCS=1 uv run --project backend --locked uvicorn finresearch.main:app --reload --host 127.0.0.1 --port 8000
```
启动后可访问 `http://127.0.0.1:8000/docs` 查看交互式 OpenAPI 接口文档。

#### 终端二：启动前端 Vite 热更新服务
```powershell
# 安装前端锁定依赖
pnpm --dir fronted install --frozen-lockfile

# Windows PowerShell 指定后端 API 地址并启动 Vite（http://127.0.0.1:5173）
$env:VITE_API_BASE="http://127.0.0.1:8000"
pnpm --dir fronted dev
```
```bash
# macOS / Linux Bash
VITE_API_BASE=http://127.0.0.1:8000 pnpm --dir fronted dev
```

### 4. 命令行运行测试、静态检查与桌面重打包

```bash
# 运行后端全量自动化回归测试（365+ 项，含架构防腐边界与数值核验红队测试）
uv run --project backend --locked --extra dev python -m pytest -q backend/tests

# 运行前端 TypeScript 严格类型检查与生产构建
pnpm --dir fronted typecheck
pnpm --dir fronted build
```

在 Windows 环境下从源码一键构建全部三种桌面包（`Setup.exe`、`Portable.exe`、`portable.zip` + 独立进程冒烟核验）：
```powershell
# 双击或命令行执行根目录脚本（产物输出至 fronted/release/）
.\build_release.bat
# 或指定自定义输出目录：
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1 -OutputDir fronted/release/v2.0.0
```

### 5. 常用环境变量速查表

| 环境变量 | 默认值 | 用途说明 |
| :--- | :--- | :--- |
| `FIN_HOST` / `FIN_BIND_ADDRESS` | `127.0.0.1` | 服务监听网卡地址（公网建议保持 `127.0.0.1` 并由 HTTPS 反向代理转发） |
| `FIN_PORT` / `PORT` | `9527` | Web 服务监听端口 |
| `FIN_ACCESS_TOKEN` | 空（本机免密） | Web 模式访问口令；设置后自动启用 `jy_session` HMAC 签名 Cookie 登录门禁与 CSRF 校验 |
| `FINRESEARCH_DATA_DIR` | 平台用户数据目录 | 自定义 SQLite 数据库、知识库与日志持久化目录（容器内固定为 `/data`） |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | DeepSeek 默认端点 | 可选默认模型服务配置（亦可在前端「设置 → 模型服务」可视化配置并加密保存） |
| `FIN_PGVECTOR_URL` | 空 | 可选 PostgreSQL（已装 `vector` 扩展）连接串，开启数据库侧余弦向量检索加速 |
| `FIN_DOCS` | `0` | 设为 `1` 时开放 `/docs` 与 `/openapi.json` 接口文档 |
| `FIN_ALLOW_STDIO_MCP` | `0`（Web 模式禁用） | 可信私有环境设为 `1` 时允许在 Web 模式加载本地子进程 `stdio` MCP 工具 |

---

## 🏗️ 系统架构与数据安全

```text
Financial-AI-Agent-Platform-/
├── fronted/                      # React 18 + TypeScript + Vite 6 + Tailwind v4 + Electron 42 工作台
│   ├── electron/                 # 桌面主进程、单实例锁、系统托盘驻留与后端 sidecar 生命周期守护
│   └── src/app/                  # 六大业务视图、K线/技术/量能图表、SSE 原子重放与增量分词流引擎
├── backend/
│   ├── run_server.py             # Electron 桌面端无窗口 sidecar 入口（原子端口握手 + 父进程看门狗）
│   ├── run_web.py                # 自部署同源 Web 独立服务入口
│   └── src/finresearch/
│       ├── engine/               # 智能体防腐层（唯一允许引用 LangChain / LangGraph / DeepAgents 的边界）
│       ├── core/                 # 纯 Python 核心域：上下文治理、NumberEvidence 数值守卫、合规闸、混合 RAG、盘后调度
│       ├── agents/               # 投研问答与深度研究双流编排、增量章节流解析与事实渲染
│       ├── tools/                # 确定性 A 股行情（多源自动降级）、财报兜底、250日风险统计、引文核验、多格式导出
│       ├── api/                  # FastAPI 路由、SSE 协议、Web 签名鉴权与 MCP 客户端管理
│       └── db/                   # SQLite WAL 异步持久化与幂等结构迁移
├── scripts/                      # Web 启动脚本、Windows 自动化打包脚本与 sidecar 独立冒烟探针
├── Dockerfile & docker-compose.yml
└── .github/workflows/release.yml # Win + Mac (arm64/x64) + GHCR Web 镜像自动化构建与发布流水线
```

### 数据流与隐私保护
- **默认完全本地落盘**：会话、研报、自选、知识库切片与配置默认保存在本地设备或自建卷（Windows 安装版为 `%APPDATA%\FinResearch`，便携版为同级 `portable-data\`，macOS 为 `~/Library/Application Support/FinResearch`，Linux 为 `~/.local/share/FinResearch`）。备份前请先完全退出应用再复制目录。
- **密钥全链路加密脱敏**：API Key、Webhook 密钥与 SMTP 密码在落盘前均经 Windows 原生 DPAPI 或持久 Fernet 密钥加密，API 响应与日志全程掩码显示（`••••••••`）。
- **出网与子进程纵深防御**：`net_guard` 统一拦截云元数据网段（`169.254.0.0/16`、`100.100.100.200`）、IPv4-mapped IPv6 绕过与内网探测；外部上传文档均在无网受限子进程（`isolated_worker`）内解析。

---

## ⚠️ 免责声明与开源许可

- **免责声明**：景渊（JingYuan）是金融数据整理与投研辅助工具，**不提供任何投资建议，不接入任何券商，也无法执行交易下单**。模型输出可能存在误差或遗漏；公开行情与财务数据可能存在延迟、修正或暂不可用。**每一个投资决策及其全部后果，均由使用者自行承担。** 市场有风险，投资需谨慎。
- **使用许可**：本项目免费开放供**个人非商业学习与投研使用**。未经授权禁止用于商业运营、付费分发或逆向工程。完整条款见 [LICENSE](LICENSE)，历次迭代记录见 [CHANGELOG.md](CHANGELOG.md)，后端接口说明见 [backend/README.md](backend/README.md)。

---

<div align="center">
<sub>景渊 JingYuan v2.0.0 · 为研究而生，不为交易。</sub>
</div>
