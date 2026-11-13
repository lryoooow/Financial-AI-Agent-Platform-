"""Load explicitly configured MCP servers and allowlisted financial tools."""

import asyncio
from functools import lru_cache

from pydantic import BaseModel, Field
from ..config import settings

from ..db.models import LocalSetting
from ..db.session import session_scope
from ..tools.secrets import decrypt_value, encrypt_value
from .client import StdioMCPClient

SETTING = "mcp-servers"
_LIMIT = asyncio.Semaphore(4)
_ACTIVE_CLIENTS: list[StdioMCPClient] = []


async def close_all_clients() -> None:
    clients, _ACTIVE_CLIENTS[:] = list(_ACTIVE_CLIENTS), []
    _client.cache_clear()
    if clients:
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)


class ServerConfig(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    command: list[str] = Field(min_length=1, max_length=32)
    allowed_tools: list[str] = Field(default_factory=list, max_length=40)
    env: dict[str, str] = Field(default_factory=dict, max_length=24)
    enabled: bool = True


async def configs() -> list[dict]:
    async with session_scope() as session:
        row = await session.get(LocalSetting, SETTING) if session else None
        return list((row.payload or {}).get("servers", [])) if row else []


async def save(servers: list[ServerConfig]) -> None:
    values = [item.model_dump() for item in servers]
    if len({item["id"] for item in values}) != len(values):
        raise ValueError("MCP 服务 ID 重复。")
    async with session_scope() as session:
        if session is None:
            raise ValueError("配置数据库不可用。")
        row = await session.get(LocalSetting, SETTING)
        existing = {item["id"]: item for item in (row.payload or {}).get("servers", [])} if row else {}
        for item in values:
            previous = existing.get(item["id"], {})
            old_env = previous.get("env", {}) if previous.get("command") == item["command"] else {}
            item["env"] = {
                key: (old_env.get(key, "") if value == "••••••••" else encrypt_value(value))
                for key, value in item["env"].items()
            }
        if row:
            row.payload = {"servers": values}
        else:
            session.add(LocalSetting(key=SETTING, payload={"servers": values}))
        await session.commit()
    await close_all_clients()


@lru_cache(maxsize=12)
def _client(command: tuple[str, ...], env: tuple[tuple[str, str], ...]) -> StdioMCPClient:
    inst = StdioMCPClient(list(command), {key: decrypt_value(value) for key, value in env})
    _ACTIVE_CLIENTS.append(inst)
    return inst


async def run(server_id: str, name: str, arguments: dict) -> dict:
    if settings.fin_web and not settings.fin_allow_stdio_mcp:
        return {"error": "此 Web 部署已关闭 stdio MCP。"}
    server = next((item for item in await configs() if item["id"] == server_id and item.get("enabled")), None)
    if server is None or name not in server.get("allowed_tools", []):
        return {"error": "MCP 服务或工具未授权。"}
    client = _client(tuple(server["command"]), tuple(sorted(server.get("env", {}).items())))
    async with _LIMIT:
        return await client.call_tool(name, arguments)
