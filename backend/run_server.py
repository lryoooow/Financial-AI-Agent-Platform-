"""服务端入口（无窗口）：供 Electron 作为 sidecar 拉起。

绑定空闲端口 → 把端口写入 %APPDATA%/FinResearch/port.txt（Electron 读这个文件拿到端口）
→ 跑 uvicorn 服务前端 + API。Electron 负责窗口，所以这里不需要 pywebview。

v1.6.0 变更（审计修复）：
- TQDM_DISABLE：akshare 内部 tqdm 进度条在 windowed 进程把 \r 乱码写满日志，全局禁用。
- CA 证书 ASCII 化：curl_cffi 按本机 ANSI 代码页编码 CAINFO 路径，安装路径含 ANSI 外
  字符（如「渊」∉ cp950）时新闻接口必崩 —— 复制 certifi 到纯 ASCII 路径并设 env 指过去。
- uvicorn.Server 显式实例化并挂到 app.state：POST /api/shutdown 可优雅停机（配合
  Electron 侧先握手再退出，lifespan 收尾/检查点 flush 不再被 TerminateProcess 跳过）。
- 父进程看门狗：Electron 崩溃后不再遗留孤儿后端进程。
- port.txt 原子写（tmp + os.replace），port.txt 非原子 truncate-写曾致双开读到半行。
- sidecar 日志 5MB 轮转，不再无限追加。
"""

# 这些必须在导入 uvicorn / finresearch 之前设置（和 main.py 一致）
import os
import socket
import sys
import warnings

# tqdm>=4.66 尊重该变量；必须在 akshare 首次 import tqdm 前设置
os.environ.setdefault("TQDM_DISABLE", "1")

warnings.filterwarnings("ignore", category=DeprecationWarning)

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import socket as _socket

import uvicorn

from finresearch.config import app_data_dir

HOST = "127.0.0.1"
PREFERRED = 9527
_SIDECAR_LOG_MAX = 5 * 1024 * 1024


class _RotatingStdio:
    """stdout/stderr 代理：超 5MB 自动轮转（.1 备份），替代无限追加的裸 open("a")。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._stream = open(path, "a", encoding="utf-8", buffering=1, errors="replace")

    def _maybe_rollover(self) -> None:
        try:
            if self._stream.tell() < _SIDECAR_LOG_MAX:
                return
            self._stream.close()
            backup = f"{self._path}.1"
            if os.path.exists(backup):
                os.remove(backup)
            if os.path.exists(self._path):
                os.replace(self._path, backup)
            self._stream = open(self._path, "a", encoding="utf-8", buffering=1, errors="replace")
        except Exception:  # noqa: BLE001
            # v1.6.3（D6）：轮转失败自恢复 —— close 与 replace 之间抛异常时流已关闭，
            # 旧实现吞掉后所有后续日志静默丢失。重开流保证日志不断流。
            try:
                self._stream = open(self._path, "a", encoding="utf-8", buffering=1, errors="replace")
            except Exception:  # noqa: BLE001
                pass

    def write(self, s) -> int:  # pragma: no cover - IO 薄包装
        try:
            n = self._stream.write(s)
            self._maybe_rollover()
            return n
        except Exception:  # noqa: BLE001
            return 0

    def flush(self) -> None:  # pragma: no cover
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self) -> bool:
        # 必须：uvicorn 彩色 formatter 初始化时调用 sys.stdout.isatty()，
        # 缺这个方法服务在启动日志配置阶段就崩（实弹冒烟抓到）。
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True


def _redirect_stdio() -> None:
    """Windowed PyInstaller sidecars may not have valid console streams."""
    try:
        os.makedirs(app_data_dir(), exist_ok=True)
        proxy = _RotatingStdio(os.path.join(app_data_dir(), "backend-sidecar.log"))
        sys.stdout = proxy  # type: ignore[assignment]
        sys.stderr = proxy  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
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


def _write_port_file(port_file: str, port: int) -> None:
    """原子写 port.txt：先写 tmp 再 os.replace，Electron 轮询不会读到半行/空文件。"""
    tmp = port_file + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(str(port))
        os.replace(tmp, port_file)
    except Exception:  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass


def _write_shutdown_token() -> str:
    """生成一次性随机停机令牌（核验修复）。

    /api/shutdown 曾可被任意本机网页用 no-cors 表单触发（简单请求免预检）关掉后端。
    现要求自定义头 X-Shutdown-Token 与本文件内容一致 —— 自定义头强制 CORS 预检，
    被（已收紧的）CORS 策略拦下。Electron 侧读同目录文件附头。
    """
    import secrets as _secrets

    token = _secrets.token_hex(16)
    path = os.path.join(app_data_dir(), "shutdown-token.txt")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(token)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass
    return token


def _ensure_ascii_cacert() -> None:
    """curl_cffi 在 Windows 上按本机 ANSI 代码页编码 CAINFO 文件路径。

    安装路径含 ANSI 之外的字符（如简体专用字「渊」在 cp950/Big5 中不存在）时，
    akshare 的新闻接口（唯一走 curl_cffi 的行情类调用）必然抛
    UnicodeEncodeError —— 2026-08 审计在本机 100% 复现。
    解法：把 certifi 证书复制到纯 ASCII 路径，并让 curl_cffi / requests 都指向它。
    纯环境操作、幂等、任何失败静默回到现状（连接层报错，不影响进程）。
    """
    try:
        import certifi
        import shutil

        src = certifi.where()
        if src.isascii():
            return
        candidates = (
            os.path.join(app_data_dir(), "cacert-ascii.pem"),
            os.path.join(os.environ.get("TEMP", "."), "cacert-ascii.pem"),
            # v1.6.3（D6）：第三候选 —— 用户名含 ANSI 外字符时 TEMP 与 AppData 路径
            # 全部非 ASCII（正是本 bug 的触发人群），系统目录永远纯 ASCII。
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp", "cacert-ascii.pem"),
        )
        for cand in candidates:
            try:
                if not cand.isascii():
                    continue
                shutil.copyfile(src, cand)
                for var in ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
                    os.environ[var] = cand
                return
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass


def _start_parent_watchdog(server: "uvicorn.Server") -> None:
    """Electron 主进程死亡后，后端自行优雅退出（不再遗留孤儿 FinResearch.exe）。

    psutil 缺失时静默跳过（降级为旧行为：等 Electron kill / 用户手清）。
    """
    try:
        import threading
        import time

        import psutil
    except Exception:  # noqa: BLE001
        return

    ppid = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(10)
            try:
                if not psutil.pid_exists(ppid):
                    print(f"parent {ppid} gone, exiting sidecar", flush=True)
                    server.should_exit = True
                    return
            except Exception:  # noqa: BLE001
                continue  # 瞬时异常（权限/临时错误）不退出看门狗，继续轮询

    threading.Thread(target=_watch, name="parent-watchdog", daemon=True).start()


def main() -> None:
    _redirect_stdio()
    # 行情代理策略：在 uvicorn/akshare 首次请求前，对东方财富/Sina 行情域名写入 NO_PROXY，
    # 绕过会被远端断开的系统/环境代理（根因 1）。纯 os.environ 操作，无重型 import。
    from finresearch.tools.market_provider import configure_market_network
    configure_market_network()
    _ensure_ascii_cacert()
    from finresearch.main import app as asgi_app

    port = _free_port()
    _write_port_file(os.path.join(app_data_dir(), "port.txt"), port)
    _write_shutdown_token()
    try:
        print(f"FIN_PORT={port}", flush=True)  # port.txt is the primary channel.
    except Exception:
        pass

    # 显式 Server 实例（而非 uvicorn.run）：挂到 app.state 供 POST /api/shutdown 优雅停机
    server = uvicorn.Server(uvicorn.Config(asgi_app, host=HOST, port=port, log_level="warning"))
    asgi_app.state.uvicorn_server = server
    _start_parent_watchdog(server)
    server.run()


if __name__ == "__main__":
    main()
