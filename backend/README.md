# FinResearch · 景渊投研后端

景渊使用 FastAPI 提供行情、投研问答、研报、知识库和定时巡检。SQLite 默认保存应用数据和检查点。模型服务通过 OpenAI 兼容接口配置，未配置时仍可使用公开行情、知识库和规则研报。

## 开发运行

从仓库根目录运行以下命令，使用 Python 3.13 和锁定的依赖。

```sh
uv sync --project backend --locked --extra dev --python 3.13
uv run --project backend --locked --extra dev python -m pytest -q backend/tests
uv run --project backend --locked uvicorn finresearch.main:app --reload --port 8000
```

在另一个终端运行 `pnpm --dir fronted dev`。Vite 默认监听 5173，前端通过 `VITE_API_BASE` 指定 API 地址。开发环境可在启动前设置 `FIN_DOCS=1` 启用接口文档。完整 Web 部署和桌面构建见 [项目说明](../README.zh-CN.md)。

Docker Compose 默认启动 `jingyuan` 服务，数据保存在命名卷。原生 Web 启动脚本和容器都安装了可选 PostgreSQL 驱动。已有 PostgreSQL 实例并启用 `vector` 扩展时，可设置 `FIN_PGVECTOR_URL` 对当前检索范围的向量执行余弦排序。文档、全文索引和向量原始数据仍由本地知识库管理。可单独通过 `DATABASE_URL` 配置应用主库及检查点连接。

## 主要接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 版本和依赖状态 |
| GET | `/api/resolve?q=&limit=` | 搜索 A 股标的 |
| GET | `/api/quote/{code}` | 实时快照与来源 |
| GET | `/api/quote/{code}/intraday` | 分时与成交量 |
| GET | `/api/quote/{code}/kline?period=weekly&adjust=qfq` | K 线、均线与复权口径 |
| POST | `/api/chart/analyze` | 按需图表速读 |
| GET | `/api/watchlist` | 自选分组与条目 |
| POST | `/api/watchlist/import-preview` | 截图、表格或文本核验预览 |
| POST | `/api/watchlist/radar` | 批量事实体检 |
| POST | `/api/chat/conversations/{id}/messages` | 投研对话 SSE |
| POST | `/api/chat/{id}/hitl-resume` | 恢复歧义确认或批量操作 |
| POST | `/api/research` | 创建并启动研究任务 |
| GET | `/api/research/{id}/stream` | 研究进度和结果 SSE |
| GET | `/api/reports/{id}/ledger` | 报告时点与当前数据对账 |
| POST | `/api/knowledge/upload` | 上传 PDF、DOCX、MD 或 TXT |
| GET | `/api/knowledge/search?q=` | 本地混合检索 |
| PUT | `/api/scheduler/config` | 保存巡检和推送配置 |

## 代码结构

- `engine/` 封装模型网关、LangChain 与 LangGraph、流程路由和事件桥。回合状态限制重复工具调用和配额。
- `core/` 维护上下文、检索、合规、外部文档隔离和定时巡检。工具调用摘要进入模型，完整数据供界面展示。
- `tools/` 负责公开数据、确定性金融计算、引文核验及研报导出。`api/` 提供 FastAPI 接口，`db/` 负责 SQLite 幂等迁移和持久化。
- `run_server.py` 供 Electron 启动本地引擎。`run_web.py` 提供浏览器同源服务。

公开行情来源可能限流、延迟或缺少估值字段。缺失数据通过来源状态或空值呈现。模型与外部检索、通知的具体数据流见 [数据流与隐私](../README.zh-CN.md#数据流与隐私)。

完整更新记录见 [CHANGELOG](../CHANGELOG.md)。
