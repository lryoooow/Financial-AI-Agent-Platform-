"""FastAPI app。

- async 单进程 uvicorn
- CORS 允许 localhost:* 与 tauri://*
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
from .tools.market_provider import configure_market_network
configure_market_network()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agents.graph import init_checkpointer
from .api.routes import router
from .config import settings
from .db.session import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("finresearch")


def _warmup_indexes() -> None:
    """后台线程预加载别名/搜索索引（首次构建约 1-3s），消除对话首轮冷启动超时。"""
    try:
        from .tools.entity import _load_alias_index, _load_search_index

        _load_search_index()
        _load_alias_index()
        logger.info("索引预加载完成（alias + search）")
    except Exception as e:  # noqa: BLE001
        logger.debug("索引预加载跳过：%s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .agents.graph import close_checkpointer  # 延迟导入避免循环

    logger.info("启动：init_db + checkpointer")
    await init_db()
    await init_checkpointer()
    # 后台预加载别名索引与搜索索引，消除对话首轮 detect_ticker 冷启动超时
    asyncio.get_event_loop().run_in_executor(None, _warmup_indexes)
    yield
    await close_checkpointer()
    logger.info("关闭。")


app = FastAPI(
    title="景渊 JingYuan · 家用金融研究助手",
    version="1.3.1",
    lifespan=lifespan,
)

# CORS：允许本地前端与 Tauri
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(http://localhost(:\d+)?|tauri://localhost|http://127\.0\.0\.1(:\d+)?)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


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
        # 让出 API 与内置文档
        if full_path.startswith(("api/", "docs", "openapi", "redoc")):
            raise HTTPException(404)
        # 防路径遍历：解析后必须仍在 _DIST 目录内，禁止 .. 跨越目录读取前端包外文件
        f = (_DIST / full_path).resolve()
        try:
            f.relative_to(_DIST.resolve())
        except ValueError:
            raise HTTPException(404)
        # （路径遍历已在上方校验，f 已 resolve）
        if full_path and f.is_file():
            return FileResponse(f)
        return FileResponse(_DIST / "index.html")
else:

    @app.get("/")
    async def root() -> dict:
        return {
            "name": "景渊 JingYuan · 家用金融研究助手",
            "version": "1.3.1",
            "llm_configured": settings.llm_configured,
            "docs": "/docs",
        }
