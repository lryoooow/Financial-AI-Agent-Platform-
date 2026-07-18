"""服务端入口（无窗口）：供 Electron 作为 sidecar 拉起。

绑定空闲端口 → 把端口写入 %APPDATA%/FinResearch/port.txt（Electron 读这个文件拿到端口）
→ 跑 uvicorn 服务前端 + API。Electron 负责窗口，所以这里不需要 pywebview。
"""

# 这些必须在导入 uvicorn / finresearch 之前设置（和 main.py 一致）
import os
import socket
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import socket as _socket

import uvicorn

from finresearch.config import app_data_dir

HOST = "127.0.0.1"
PREFERRED = 9527
_STDIO_HANDLE = None


def _redirect_stdio() -> None:
    """Windowed PyInstaller sidecars may not have valid console streams."""
    global _STDIO_HANDLE
    try:
        os.makedirs(app_data_dir(), exist_ok=True)
        _STDIO_HANDLE = open(
            os.path.join(app_data_dir(), "backend-sidecar.log"),
            "a",
            encoding="utf-8",
            buffering=1,
        )
        sys.stdout = _STDIO_HANDLE
        sys.stderr = _STDIO_HANDLE
    except Exception:  # noqa: BLE001
        try:
            _STDIO_HANDLE = open(os.devnull, "w", encoding="utf-8")
            sys.stdout = _STDIO_HANDLE
            sys.stderr = _STDIO_HANDLE
        except Exception:  # noqa: BLE001
            pass


def _free_port() -> int:
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, PREFERRED))
            return PREFERRED
        except OSError:
            s.bind((HOST, 0))
            return s.getsockname()[1]


def main() -> None:
    _redirect_stdio()
    # 行情代理策略：在 uvicorn/akshare 首次请求前，对东方财富/Sina 行情域名写入 NO_PROXY，
    # 绕过会被远端断开的系统/环境代理（根因 1）。纯 os.environ 操作，无重型 import。
    from finresearch.tools.market_provider import configure_market_network
    configure_market_network()
    from finresearch.main import app as asgi_app

    port = _free_port()
    port_file = os.path.join(app_data_dir(), "port.txt")
    try:
        with open(port_file, "w") as f:
            f.write(str(port))
    except Exception:  # noqa: BLE001
        pass
    try:
        print(f"FIN_PORT={port}", flush=True)  # port.txt is the primary channel.
    except Exception:
        pass
    uvicorn.run(asgi_app, host=HOST, port=port, log_level="warning")


if __name__ == "__main__":
    main()
