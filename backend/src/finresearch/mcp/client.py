"""Bounded stdio JSON-RPC with concurrent draining of both process pipes."""

import asyncio
import json
import os
import queue
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

_STDERR_TAIL = 16 * 1024
_MAX_LINE = 1024 * 1024
_PASSTHROUGH = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP", "TMPDIR", "USERPROFILE", "HOME"}
_BLOCKED_ENV = {
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "RUBYOPT", "PERL5OPT", "BASH_ENV", "ENV",
}


class StdioMCPClient:
    def __init__(self, command: list[str], env: dict[str, str] | None = None, max_concurrency: int = 4) -> None:
        if not command or len(command) > 32:
            raise ValueError("MCP 启动命令无效。")
        self.command = list(command)
        safe_user_env = {
            str(k): str(v) for k, v in (env or {}).items()
            if str(k).upper() not in _BLOCKED_ENV and "\x00" not in str(k) and "\x00" not in str(v)
        }
        self.env = {**{k: v for k, v in os.environ.items() if k.upper() in _PASSTHROUGH}, **safe_user_env}
        self._slots = asyncio.Semaphore(max_concurrency)

    async def list_tools(self, timeout: float = 10) -> dict:
        return await self._request("tools/list", {}, timeout)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout: float = 20) -> dict:
        return await self._request("tools/call", {"name": tool_name, "arguments": arguments}, timeout)

    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        async with self._slots:
            session = _Session(self.command, self.env)
            try:
                async with asyncio.timeout(timeout):
                    initialized = await session.ask(1, "initialize", {
                        "protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "JingYuan", "version": "2.0"},
                    })
                    if "error" in initialized:
                        raise ValueError("MCP 服务器初始化失败。")
                    await session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    response = await session.ask(2, method, params)
                    if "error" in response:
                        return {"error": "MCP 工具返回错误。"}
                    return response.get("result") or {}
            finally:
                await asyncio.to_thread(session.close)


class _Session:
    def __init__(self, command: list[str], env: dict[str, str]):
        command = list(command)
        self.container = ""
        if Path(command[0]).stem.lower() == "docker" and command[1:2] == ["run"]:
            if any(arg == "--name" or arg.startswith("--name=") for arg in command[2:]):
                raise ValueError("MCP Docker 容器名称由应用分配。")
            self.container = "jingyuan-mcp-" + uuid.uuid4().hex[:12]
            command[2:2] = ["--rm", "--name", self.container]
        self.docker = command[0] if self.container else ""
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, creationflags=flags, start_new_session=os.name != "nt",
        )
        self.closed = threading.Event()
        self.responses: queue.Queue = queue.Queue(maxsize=32)
        self.stderr_tail = bytearray()
        self.threads = (
            threading.Thread(target=self._stdout, daemon=True),
            threading.Thread(target=self._stderr, daemon=True),
        )
        for thread in self.threads:
            thread.start()

    def _stdout(self):
        stream = self.process.stdout
        try:
            while not self.closed.is_set():
                line = stream.readline(_MAX_LINE + 1)
                if not line or len(line) > _MAX_LINE:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict) and "id" in message:
                    self.responses.put(message, timeout=0.2)
        except (OSError, queue.Full):
            pass
        finally:
            try:
                self.responses.put_nowait(None)
            except queue.Full:
                pass

    def _stderr(self):
        stream = self.process.stderr
        try:
            while not self.closed.is_set():
                chunk = stream.read1(4096)
                if not chunk:
                    return
                self.stderr_tail.extend(chunk)
                del self.stderr_tail[:-_STDERR_TAIL]
        except OSError:
            pass

    async def send(self, message: dict):
        def write():
            line = json.dumps(message, ensure_ascii=False).encode() + b"\n"
            if len(line) > _MAX_LINE:
                raise ValueError("MCP 请求过大。")
            self.process.stdin.write(line)
            self.process.stdin.flush()
        await asyncio.to_thread(write)

    async def ask(self, request_id: int, method: str, params: dict) -> dict:
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                response = await asyncio.to_thread(self.responses.get, True, 0.25)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise ValueError("MCP 服务器提前退出。")
                continue
            if response is None:
                raise ValueError("MCP 服务器关闭了输出。")
            if response.get("id") == request_id:
                return response

    def close(self):
        self.closed.set()
        if self.process.poll() is None:
            try:
                if os.name == "nt":
                    self.process.kill()
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if self.container:
            try:
                subprocess.run(
                    [self.docker, "rm", "-f", self.container], timeout=5, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        for thread in self.threads:
            thread.join(timeout=0.3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
