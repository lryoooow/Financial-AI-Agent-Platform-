"""FastAPI app。

- async 单进程 uvicorn
- CORS 仅显式放行本地 vite dev（5173）；生产为后端同源伺服前端
- lifespan：init_db（create_all）+ 初始化检查点
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

# Windows：psycopg(async) / SQLAlchemy async 不能用默认 ProactorEventLoop，
# 必须切到 SelectorEventLoop。uvicorn/ASGI 启动前设置全局 policy 即生效。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 兜底所有 socket（akshare/requests/httpx）—— 外部数据源不可达时快速失败，
# 避免单次行情请求挂死整个服务。各工具内已有 try/except 转 provenance.error。
import socket
socket.setdefaulttimeout(15)

# 行情代理策略（根因 1）：在导入路由/工具（可能触发 akshare 行为）之前，
# 对东方财富/Sina 行情域名写入 NO_PROXY，绕过会被远端断开的系统/环境代理。
from . import __version__
from .tools.market_provider import configure_market_network
configure_market_network()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agents.graph import init_checkpointer
from .api.knowledge_routes import router as knowledge_router
from .api.routes import router
from .config import settings
from .db.session import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("finresearch")


def _warmup_indexes() -> None:
    """后台线程预加载搜索索引（首次构建约 1-2s），消除对话首轮冷启动超时。

    v1.6.0（审计 E1）：别名索引（_load_alias_index）随标的检测器一起废弃，
    构建它纯属启动开销，已整体移除。
    """
    try:
        from .tools.entity import _load_search_index

        _load_search_index()
        logger.info("索引预加载完成（search）")
    except Exception as e:  # noqa: BLE001
        logger.debug("索引预加载跳过：%s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .agents.graph import close_checkpointer  # 延迟导入避免循环
    from .api.routes import sweep_stale_research_tasks

    logger.info("启动：init_db + checkpointer")
    await init_db()
    # v1.6.0（审计 B4）：启动清扫上一进程遗留的 pending/running 研究任务（僵尸终结）
    try:
        await sweep_stale_research_tasks()
    except Exception as e:  # noqa: BLE001
        logger.warning("启动清扫失败（不影响启动）：%s", e)
    await init_checkpointer()
    # 后台预加载搜索索引，消除对话首轮冷启动超时
    asyncio.get_event_loop().run_in_executor(None, _warmup_indexes)
    # v1.8.0：本地知识库建库 + 存量报告回填（后台进行，不拖慢启动）
    from .knowledge.service import init_and_backfill

    _kb_task = asyncio.create_task(init_and_backfill())
    yield
    if not _kb_task.done():
        _kb_task.cancel()
    await close_checkpointer()
    logger.info("关闭。")


# v1.6.0（审计 D6）：生产关闭 /docs /redoc /openapi.json（API 自描述不随包分发）；
# 开发需要时设 FIN_DOCS=1。
_docs = bool(os.environ.get("FIN_DOCS"))

app = FastAPI(
    title="景渊 JingYuan · 家用金融研究助手",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

# v1.6.0（审计 B1）：CORS 收紧。此前 allow_origin_regex 放行任意 localhost:* 源 ——
# 本机任何端口上的网页都能跨域读走 /api/local-settings/model-configs 里的明文密钥。
# v1.6.3（E）：打包生产不放行任何来源（同源伺服无需 CORS）；FIN_DEV=1 才开 vite dev 5173。
app.add_middleware(
    CORSMiddleware,
    allow_origins=(["http://localhost:5173", "http://127.0.0.1:5173"]
                   if os.environ.get("FIN_DEV") else []),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _api_no_store_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(router)
app.include_router(knowledge_router)


# ── 前端静态托管（打包后由后端一并提供 UI；dev 无 dist 时跳过）──
def _find_frontend_dist():
    from pathlib import Path

    cand = []
    if settings.frontend_dist:
        cand.append(Path(settings.frontend_dist))
    meipass = os.environ.get("_MEIPASS")  # PyInstaller 运行时资源根
    if meipass:
        cand.append(Path(meipass) / "static")
    cand.append(Path(__file__).resolve().parent / "static")
    for c in cand:
        try:
            if (c / "index.html").is_file():
                return c
        except Exception:  # noqa: BLE001
            pass
    return None


_DIST = _find_frontend_dist()
if _DIST:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    logger.info("托管前端构建产物：%s", _DIST)
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def _spa(full_path: str):
        # 让出 API 与内置文档（含裸 /api 路径）
        if full_path == "api" or full_path.startswith(("api/", "docs", "openapi", "redoc")):
            raise HTTPException(404)
        # 防路径遍历：解析后必须仍在 _DIST 目录内，禁止 .. 跨越目录读取前端包外文件
        f = (_DIST / full_path).resolve()
        try:
            f.relative_to(_DIST.resolve())
        except ValueError:
            raise HTTPException(404)
        # （路径遍历已在上方校验，f 已 resolve）
        # v1.8.0：入口页必须 no-cache —— 此前不带缓存头，Electron 的 Chromium 按 Last-Modified
        # 启发式缓存旧 index.html（及其引用的旧哈希脚本），升级安装后窗口仍显示旧界面。
        # 带哈希的 /assets 走上面的 StaticFiles，内容变则文件名变，可放心缓存。
        no_cache = {"Cache-Control": "no-cache, no-store, must-revalidate"}
        if full_path and f.is_file():
            return FileResponse(f, headers=no_cache if f.suffix in {".html", ".json"} else None)
        return FileResponse(_DIST / "index.html", headers=no_cache)
else:

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "景渊 JingYuan · 家用金融研究助手",
            "version": __version__,
            "llm_configured": settings.llm_configured,
            "docs": "/docs" if _docs else None,
        }
