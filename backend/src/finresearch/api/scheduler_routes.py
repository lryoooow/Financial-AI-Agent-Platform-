"""Configuration and explicit test delivery for the local post-close scheduler."""

import asyncio
import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..core.scheduler.notifier import send_channel
from ..core.scheduler.post_close import load_config, recent_runs, save_config

router = APIRouter(prefix="/api/scheduler")
Channel = Literal["wework", "feishu", "dingtalk", "smtp"]


class ChannelConfig(BaseModel):
    enabled: bool = False
    webhook: str = Field(default="", max_length=2048)
    secret: str = Field(default="", max_length=512)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=465, ge=1, le=65535)
    tls: Literal["ssl", "starttls"] = "ssl"
    username: str = Field(default="", max_length=254)
    password: str = Field(default="", max_length=512)
    sender: str = Field(default="", max_length=254)
    recipient: str = Field(default="", max_length=512)

    @field_validator("host", "username", "sender", "recipient")
    @classmethod
    def valid_mail_field(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("邮件字段不能包含换行。")
        return value.strip()


class SchedulerConfig(BaseModel):
    enabled: bool = False
    run_time: str = "15:30"
    group_id: str = Field(default="", max_length=64)
    attach_pdf: bool = False
    channels: dict[Channel, ChannelConfig] = Field(default_factory=dict)

    @field_validator("run_time")
    @classmethod
    def after_close(cls, value: str) -> str:
        if not re.fullmatch(r"(1[5-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("时间需为 15:00 至 23:59（北京时间）。")
        return value


class TestPush(BaseModel):
    channel: Channel


@router.get("/config")
async def scheduler_config() -> dict:
    return {**await load_config(public=True), "runs": await recent_runs()}


@router.put("/config")
async def put_scheduler_config(req: SchedulerConfig) -> dict:
    try:
        await save_config(req.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return await scheduler_config()


@router.post("/test-push")
async def test_push(req: TestPush) -> dict:
    config = await load_config()
    entry = config["channels"].get(req.channel)
    if not entry or not entry.get("enabled"):
        raise HTTPException(422, "请先保存并启用该通知渠道。")
    return await asyncio.to_thread(send_channel, req.channel, entry, "景渊通知测试", "此消息用于验证收盘巡检通知配置。")
