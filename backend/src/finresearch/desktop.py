"""桌面启动器（真软件入口）。

后台线程跑 uvicorn（127.0.0.1:port），前台 pywebview 开独立应用窗口（Edge WebView2，
Win10/11 自带）。关闭窗口 → 停服务 → 退出。pywebview 不可用时回退默认浏览器。

这是 PyInstaller 打包的入口（见 run_desktop.py / finresearch.spec）。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import webbrowser

from .config import settings

logger = logging.getLogger(__name__)


def _free_port(host: str, preferred: int) -> int:
    """优先用 preferred；被占用就让 OS 分配空闲端口（彻底避免端口冲突）。"""
    import socket as _s

    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as s:
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            s.bind((host, 0))
            return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def main() -> None:
    import uvicorn

    host = "127.0.0.1"
    port = _free_port(host, settings.port)  # 自动避让被占端口（如 8000 被别的服务占用）
    url = f"http://{host}:{port}"

    # 后台线程跑 ASGI 服务（单进程家用）
    server = uvicorn.Server(
        uvicorn.Config("finresearch.main:app", host=host, port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()

    if not _wait_for_server(url):
        logger.warning("服务未在超时内就绪，仍尝试打开窗口。")

    # 前台：pywebview 独立窗口（真软件观感）
    try:
        import webview  # pywebview，懒加载（仅桌面入口需要）

        webview.create_window(
            "景渊 · 金融研究助手", url, width=1280, height=820, min_size=(960, 600)
        )
        webview.start()  # 阻塞到窗口关闭
        server.should_exit = True
        time.sleep(0.5)
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        logger.warning("pywebview 不可用，回退默认浏览器：%s", e)
        webbrowser.open(url)
        try:
            while not server.should_exit:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True


if __name__ == "__main__":
    main()
