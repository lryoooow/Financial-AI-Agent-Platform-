"""Settings for explicitly configured stdio integrations."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..mcp import service
from ..config import settings

router = APIRouter(prefix="/api/mcp")


class ConfigRequest(BaseModel):
    servers: list[service.ServerConfig] = Field(max_length=12)


@router.get("/servers")
async def list_servers() -> dict:
    editable = not settings.fin_web or settings.fin_allow_stdio_mcp
    return {"editable": editable, "servers": [
        {**item, "env": {key: "••••••••" if value else "" for key, value in item.get("env", {}).items()}}
        for item in (await service.configs() if editable else [])
    ]}


@router.put("/servers")
async def save_servers(request: ConfigRequest) -> dict:
    if settings.fin_web and not settings.fin_allow_stdio_mcp:
        raise HTTPException(403, "此 Web 部署已关闭 stdio MCP。")
    try:
        await service.save(request.servers)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True}
