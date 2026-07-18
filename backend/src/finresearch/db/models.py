"""ORM 模型：ResearchTask / Report。

家用：仅存任务元数据与最终报告；中间状态由 LangGraph 检查点保存。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class ResearchTask(Base):
    __tablename__ = "research_task"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(32), index=True)
    template: Mapped[str] = mapped_column(String(32), default="equity_deep")
    query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/done/error
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 前端选择的模型配置 {base_url, api_key, model}（SSE 是 GET，无法带 body；落库持久化供 stream 取用）
    llm_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 可恢复字段（1.2.0）：当前阶段 / 阶段事件流 / 各阶段耗时 / 关联报告 id
    stage: Mapped[str] = mapped_column(String(32), default="pending")  # pending/entity/research/market/report/done/error
    progress_events: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # [{node,label,timings,ts,gap?}]
    timings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # {entity_ms,research_ms,...,llm_summary_ms}
    report_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Report(Base):
    __tablename__ = "report"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(32))
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    template: Mapped[str] = mapped_column(String(32), default="equity_deep")
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # 完整 ReportDoc
    html: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    as_of: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatConversation(Base):
    __tablename__ = "chat_conversation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(160), default="新对话")
    snapshot_interval: Mapped[int] = mapped_column(Integer, default=8)
    model_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    archived_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatDelta(Base):
    __tablename__ = "chat_delta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="delta")  # delta/snapshot
    role: Mapped[str] = mapped_column(String(16), default="user")
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatContextSummary(Base):
    """对话压缩事件：原始 ChatDelta 不改写，只记录当前有效摘要与原文指针。"""

    __tablename__ = "chat_context_summary"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cutoff_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    history_path: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LocalSetting(Base):
    __tablename__ = "local_setting"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
