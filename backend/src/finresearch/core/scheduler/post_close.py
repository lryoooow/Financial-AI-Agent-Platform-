"""One durable scheduled run per trading day in Asia/Shanghai."""

import asyncio
import datetime as dt
import logging
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ...db.models import LocalSetting, Report, ResearchTask, ScheduledRun
from ...db.session import session_scope
from ...tools import now_cst
from ...tools.batch_radar import batch_radar
from ...tools.context import is_trading_day
from ...tools.secrets import decrypt_value, encrypt_value, is_masked_secret, mask_secret
from ..guardrails.isolated_worker import render_export
from .notifier import send_channel, validate_webhook

logger = logging.getLogger(__name__)
_KEY = "post-close-scheduler"
_SECRETS = {"webhook", "secret", "password"}
_CHANNELS = ("wework", "feishu", "dingtalk", "smtp")
_SETTINGS_LOCK = asyncio.Lock()


async def load_config(*, public: bool = False) -> dict:
    async with session_scope() as session:
        row = await session.get(LocalSetting, _KEY) if session else None
    stored = row.payload if row and row.payload else {}
    channels = {}
    for channel, entry in (stored.get("channels") or {}).items():
        channels[channel] = {
            key: (mask_secret(value) if public else decrypt_value(value)) if key in _SECRETS else value
            for key, value in entry.items()
        }
    return {"enabled": False, "run_time": "15:30", "group_id": "", "attach_pdf": False, **stored, "channels": channels}


async def save_config(settings: dict) -> None:
    async with _SETTINGS_LOCK, session_scope() as session:
        if session is None:
            raise RuntimeError("本地数据库不可用。")
        row = await session.get(LocalSetting, _KEY)
        previous = (row.payload or {}).get("channels", {}) if row else {}
        channels = {}
        for channel in _CHANNELS:
            if channel not in settings["channels"]:
                continue
            entry = settings["channels"][channel]
            old = previous.get(channel, {})
            same_smtp = all(entry.get(key) == old.get(key) for key in ("host", "port", "tls", "username"))
            old_wh_plain = decrypt_value(old.get("webhook", "")) if old.get("webhook") else ""
            raw_wh = str(entry.get("webhook") or "")
            same_webhook = is_masked_secret(raw_wh) or raw_wh == old_wh_plain
            stored = {}
            for key, value in entry.items():
                if key in _SECRETS and isinstance(value, str):
                    if not is_masked_secret(value):
                        stored[key] = encrypt_value(value)
                    elif key == "password" and not same_smtp:
                        stored[key] = ""
                    elif key == "secret" and not same_webhook:
                        stored[key] = ""
                    else:
                        stored[key] = old.get(key, "")
                else:
                    stored[key] = value
            if stored.get("enabled"):
                if channel == "smtp":
                    if not all(stored.get(key) for key in ("host", "sender", "recipient")):
                        raise ValueError("SMTP 需要主机、发件人和收件人。")
                else:
                    validate_webhook(channel, decrypt_value(stored.get("webhook", "")))
            channels[channel] = stored
        payload = {**settings, "channels": channels}
        if row:
            row.payload = payload
        else:
            session.add(LocalSetting(key=_KEY, payload=payload))
        await session.commit()


async def recent_runs() -> list[dict]:
    async with session_scope() as session:
        if session is None:
            return []
        rows = (await session.scalars(select(ScheduledRun).order_by(ScheduledRun.created_at.desc()).limit(15))).all()
        return [{"id": row.id, "date": row.run_date, "status": row.status, "report_id": row.summary_report_id,
                 "channels": row.push_channels_json, "error": row.error_msg} for row in rows]


async def _finish(run_id: str, **values) -> None:
    async with session_scope() as session:
        row = await session.get(ScheduledRun, run_id) if session else None
        if row is not None:
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()


async def _claim(day: str) -> str | None:
    async with session_scope() as session:
        if session is None:
            return None
        existing = await session.scalar(select(ScheduledRun).where(ScheduledRun.run_date == day))
        if existing is not None:
            if existing.status in ("interrupted", "error"):
                existing.status = "running"
                existing.error_msg = None
                await session.commit()
                return existing.id
            return None
        run = ScheduledRun(id=uuid4().hex, run_date=day, trigger_type="scheduled")
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            return None
        return run.id


def due(now: dt.datetime, config: dict) -> bool:
    return bool(config.get("enabled") and is_trading_day(now) and now.strftime("%H:%M") >= config["run_time"])


async def _run_recap() -> tuple[str, dict]:
    from ...api import routes

    outcome = await routes.create_research(routes.ResearchRequest(template="market_recap"))
    task_id = outcome["task_id"]
    for _ in range(90):
        async with session_scope() as session:
            task = await session.get(ResearchTask, task_id) if session else None
            if task and task.report_id:
                report = await session.get(Report, task.report_id)
                if report:
                    return report.id, report.payload or {}
            if task is None or task.status == "error":
                raise RuntimeError("收盘复盘生成失败。")
        await asyncio.sleep(2)
    live = routes._LIVE_RUNS.get(task_id)
    if live and live.bg_task and not live.bg_task.done():
        live.bg_task.cancel()
    raise RuntimeError("收盘复盘等待超时。")


async def _run_claimed(run_id: str, config: dict) -> None:
    from ...api.watchlist_routes import group_symbols

    channels = {channel: entry for channel, entry in config["channels"].items() if entry.get("enabled")}
    symbols = await group_symbols(config["group_id"])
    report_id, doc = await _run_recap()
    rows = []
    for offset in range(0, len(symbols), 50):
        rows.extend((await batch_radar(symbols[offset:offset + 50]))["rows"])
    events = [f"{row['name']} · {' / '.join(row['groups'])}" for row in rows if row["groups"] not in (["平稳运行"], ["数据不足"])]
    body = "\n".join([str(doc.get("as_of") or ""), str(doc.get("summary") or ""), f"自选核验 {len(rows)} 只", *events[:8],
                      "内容用于事实核对和研究参考。"])
    attachment = None
    attachment_error = False
    if config["attach_pdf"] and "smtp" in channels:
        try:
            attachment = await render_export("pdf", doc)
        except ValueError:
            attachment_error = True
    statuses = {}
    await _finish(run_id, summary_report_id=report_id, radar_json={"rows": rows}, push_channels_json=statuses)
    for channel, entry in channels.items():
        statuses[channel] = await asyncio.to_thread(send_channel, channel, entry, "每日收盘客观巡检摘要", body,
                                                    attachment if channel == "smtp" else None)
        await _finish(run_id, push_channels_json=dict(statuses))
    complete = all(value["ok"] for value in statuses.values()) and not attachment_error
    await _finish(run_id, status="done" if complete else "partial",
                  error_msg="PDF 附件生成失败。" if attachment_error else None)


async def scheduler_loop() -> None:
    async with session_scope() as session:
        if session is not None:
            await session.execute(update(ScheduledRun).where(ScheduledRun.status == "running")
                                  .values(status="interrupted", error_msg="上次进程退出，巡检未完成。"))
            await session.commit()
    observed = ""
    while True:
        run_id = None
        try:
            config, now = await load_config(), now_cst()
            day = now.date().isoformat()
            if day != observed and due(now, config):
                run_id = await _claim(day)
                if run_id:
                    await _run_claimed(run_id, config)
                    observed = day
                else:
                    observed = day
        except asyncio.CancelledError:
            if run_id:
                await _finish(run_id, status="interrupted", error_msg="应用关闭，巡检中断。")
            raise
        except Exception:
            logger.exception("收盘定时巡检失败")
            if run_id:
                await _finish(run_id, status="error", error_msg="巡检或复盘数据处理失败。")
        await asyncio.sleep(30)
