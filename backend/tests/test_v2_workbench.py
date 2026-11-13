"""End-to-end regression fixtures for the watchlist and daily close workflow."""

import asyncio
import base64
import datetime as dt
import io
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from finresearch.api import routes, scheduler_routes, watchlist_routes
from finresearch.core import report_ledger, watchlist_import
from finresearch.core.scheduler import notifier, post_close
from finresearch.db import session
from finresearch.db.models import Base, ResearchTask
from finresearch.main import app
from finresearch.tools.entity import _seed_name_map, stock_ticker
from finresearch.tools import batch_radar, now_cst


async def _database(monkeypatch, path):
    engine = create_async_engine("sqlite+aiosqlite:///" + path.as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(session, "_engine", engine)
    monkeypatch.setattr(session, "_maker", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(session, "_tried", True)
    return engine


def _symbols(n):
    output = []
    for code in _seed_name_map():
        try:
            output.append(stock_ticker(code))
        except ValueError:
            continue
        if len(output) == n:
            break
    return output


def test_text_xlsx_and_vision_are_checked_against_local_catalogue(monkeypatch):
    from openpyxl import Workbook
    from PIL import Image

    async def run():
        result = await watchlist_import.import_preview("text", "观察 贵州茅台 / 002594\n平安\n999999.SH", "", None)
        assert {item["code"] for item in result["verified"]} == {"600519.SH", "002594.SZ"}
        assert result["ambiguous"] and "999999.SH" in result["unmatched"]
        book = Workbook()
        book.active.append(["股票代码", "股票简称"])
        book.active.append(["300750", "宁德时代"])
        excel = io.BytesIO()
        book.save(excel)
        result = await watchlist_import.import_preview("table", base64.b64encode(excel.getvalue()).decode(), "自选.xlsx", None)
        assert [item["code"] for item in result["verified"]] == ["300750.SZ"]

        image = io.BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="PNG")

        async def recognize(_messages):
            return SimpleNamespace(content='["贵州茅台","999999.SH"]')

        monkeypatch.setattr(watchlist_import, "build_llm", lambda **_: SimpleNamespace(ainvoke=recognize))
        data = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
        result = await watchlist_import.import_preview("image", data, "", None)
        assert [item["code"] for item in result["verified"]] == ["600519.SH"]
        assert result["unmatched"] == ["999999.SH"]

    asyncio.run(run())


def test_watchlist_duplicate_order_and_batch_approval(monkeypatch, tmp_path):
    async def quote(_code):
        return {"price": 12.34}

    async def run():
        engine = await _database(monkeypatch, tmp_path / "watchlist.db")
        monkeypatch.setattr(watchlist_routes, "_quote", quote)
        launched = []
        monkeypatch.setattr(routes, "_start_batch", lambda task_ids: launched.extend(task_ids))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                group = (await client.get("/api/watchlist")).json()["groups"][0]
                assert group["name"] == "核心自选"
                codes = _symbols(20)
                body = {"group_id": group["id"], "symbols": codes}
                left, right = await asyncio.gather(client.post("/api/watchlist", json=body), client.post("/api/watchlist", json=body))
                assert left.is_success and right.is_success
                assert len(left.json()["items"]) + len(right.json()["items"]) == 20
                stored = (await client.get("/api/watchlist")).json()["groups"][0]["items"]
                assert len(stored) == 20 and {item["added_price"] for item in stored} == {12.34}
                ids = [item["id"] for item in reversed(stored)]
                assert (await client.patch("/api/watchlist", json={"kind": "order", "group_id": group["id"], "ids": ids})).is_success
                assert [item["id"] for item in (await client.get("/api/watchlist")).json()["groups"][0]["items"]] == ids

                batch_response = await client.post("/api/research/batch", json={"symbols": codes})
                assert batch_response.is_success, batch_response.text
                batch = batch_response.json()
                assert batch["status"] == "awaiting_hitl" and not launched
                conversation = batch["conversation_id"]
                checkpoint = batch["approval"]["checkpoint_id"]
                resume_path = f"/api/chat/{conversation}/hitl-resume"
                outcome = await client.post(resume_path, json={"checkpoint_id": checkpoint, "action": "approve"})
                assert outcome.is_success, outcome.text
                assert len(launched) == len(outcome.json()["task_ids"]) == 20
                assert (await client.post(resume_path, json={"checkpoint_id": checkpoint, "action": "approve"})).status_code == 409
                history = (await client.get(f"/api/chat/conversations/{conversation}")).json()["messages"]
                assert history[-1]["approval"]["status"] == "approved"
                async with session.session_scope() as db:
                    assert (await db.get(ResearchTask, launched[0])).status == "pending"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_ledger_computes_only_aligned_returns(monkeypatch):
    dates = ["2026-04-07", "2026-04-08", "2026-04-09"]
    snapshot = {"report_price": 100, "report_pb": 2, "price_as_of": dates[0], "fin_period": "2025-12-31"}
    def bars(values):
        return {"data": [{"date": day, "close": value} for day, value in zip(dates, values)], "adjust": "qfq"}
    monkeypatch.setattr(report_ledger, "get_realtime_quote", lambda _: {"price": 110, "pb": 2.2})
    monkeypatch.setattr(report_ledger, "get_financial_indicators", lambda _: {"financials": {"periods": ["2026-03-31"]}})
    monkeypatch.setattr(report_ledger, "index_quote", lambda *_: {"price": 105})
    monkeypatch.setattr(report_ledger, "valuation_snapshot", lambda _: {})
    monkeypatch.setattr(report_ledger, "get_kline", lambda *_: bars([100, 90, 110]))
    monkeypatch.setattr(report_ledger, "tencent_kline", lambda *_, **__: bars([100, 102, 105]))
    ledger = asyncio.run(report_ledger.read_report_ledger("600519.SH", snapshot, {}))
    assert ledger["snapshot"] == snapshot
    assert ledger["comparison"]["excess_pp"] == 5
    assert ledger["comparison"]["max_drawdown_pct"] == -10
    assert ledger["comparison"]["new_financial_period"] is True


def test_twenty_watchlist_radar_rows_use_only_provider_facts(monkeypatch):
    first = dt.date(2026, 4, 1)
    points = [{"date": str(first + dt.timedelta(days=i)), "close": 100 + i / 2, "volume": 1000 + i} for i in range(70)]
    source = {"source": "fixture", "fetched_at": "2026-06-10 15:30:00"}
    bars = {"data": points, "provenance": source}
    monkeypatch.setattr(batch_radar, "get_realtime_quote", lambda code: {"name": code, "price": 134.5, "changePct": 2})
    monkeypatch.setattr(batch_radar, "get_kline", lambda *_: bars)
    monkeypatch.setattr(batch_radar, "tencent_kline", lambda *_, **__: bars)
    monkeypatch.setattr(batch_radar, "valuation_snapshot", lambda _: {"pe_ttm": 22.0, "pe_ttm_percentile": 40.0, "provenance": source})
    monkeypatch.setattr(batch_radar, "compute_indicators_from_kline", lambda *_: {"data": [{"name": "RSI(14)", "value": "55"}]})
    monkeypatch.setattr(batch_radar, "_cashflow", lambda *_: {"value": 12e8, "provenance": source})
    monkeypatch.setattr(batch_radar, "get_financial_indicators", lambda _: {"financials": {
        "periods": ["2026-03-31"], "years": ["2026一季报"], "rows": [
            {"name": "营收同比", "values": ["8.5%"]}, {"name": "净利润同比", "values": ["-1.0%"]},
        ],
    }, "provenance": source})
    item = {"title": "样本公告", "time": now_cst().isoformat()}
    monkeypatch.setattr(batch_radar, "search_news", lambda *_, **__: {"items": [item], "provenance": source})
    rows = asyncio.run(batch_radar.batch_radar(_symbols(20)))["rows"]
    assert len(rows) == 20
    assert all(row["earnings"]["revenue_yoy"] == 8.5 and row["earnings"]["operating_cashflow"] == 12e8 for row in rows)
    assert all(row["events"]["count"] == 1 and row["risk"]["beta"] == pytest.approx(1) for row in rows)


def test_scheduler_claim_webhook_smtp_and_encryption(monkeypatch, tmp_path):
    received = []
    original_client = httpx.Client

    def webhook(request):
        assert request.url.host == "qyapi.weixin.qq.com"
        assert "景渊通知测试" in request.content.decode()
        received.append("webhook")
        return httpx.Response(200, json={"errcode": 0})

    class Mail:
        def __init__(self, host, port, **_):
            assert (host, port) == ("mail.example.test", 465)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def login(self, username, password):
            assert (username, password) == ("fixture", "synthetic-password")
        def send_message(self, message):
            assert message["To"] == "test@example.test"
            received.append("smtp")
            return {}

    monkeypatch.setattr(notifier.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(webhook), **kwargs))
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", Mail)
    monkeypatch.setenv("FIN_SECRET_KEY", "testing-only-key")
    async def run():
        engine = await _database(monkeypatch, tmp_path / "scheduler.db")
        try:
            config = {"enabled": True, "run_time": "15:30", "channels": {
                "wework": {"enabled": True, "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic"},
                "smtp": {"enabled": True, "host": "mail.example.test", "port": 465, "sender": "sender@example.test",
                         "recipient": "test@example.test", "username": "fixture", "password": "synthetic-password"},
            }}
            result = await scheduler_routes.put_scheduler_config(scheduler_routes.SchedulerConfig.model_validate(config))
            assert "synthetic-password" not in repr(result) and "https://" not in repr(result)
            assert (await scheduler_routes.test_push(scheduler_routes.TestPush(channel="wework")))["ok"]
            assert (await scheduler_routes.test_push(scheduler_routes.TestPush(channel="smtp")))["ok"]
            assert post_close.due(dt.datetime(2026, 11, 12, 15, 31), config)
            assert not post_close.due(dt.datetime(2026, 11, 14, 16), config)
            assert await post_close._claim("2026-11-12")
            assert await post_close._claim("2026-11-12") is None
        finally:
            await engine.dispose()
    asyncio.run(run())
    assert received == ["webhook", "smtp"]
