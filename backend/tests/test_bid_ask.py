"""审计 P0-A2：五档盘口三重契约错误（此前 100% 死亡）。

已核对打包内 akshare 源码 stock_ask_bid_em.py：
- 接收纯 6 位代码，自行拼 secid（传 "sh600519" 会发出非法 secid=0.sh600519）；
- 返回 DataFrame 是 item/value 两列；
- 行键是英文 sell_N / sell_N_vol / buy_N / buy_N_vol + 最新/均价 等中文行。
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _fake_df() -> pd.DataFrame:
    rows = []
    # 卖五→卖一（价格递减），量单位为股（akshare 已 *100）
    for i, (p, v) in enumerate([(1300.0, 1200), (1299.0, 3400), (1298.0, 5600), (1297.0, 7800), (1296.0, 9900)]):
        rows.append((f"sell_{5 - i}", p))
        rows.append((f"sell_{5 - i}_vol", v))
    for i, (p, v) in enumerate([(1295.0, 1100), (1294.0, 2200), (1293.0, 3300), (1292.0, 4400), (1291.0, 5500)]):
        rows.append((f"buy_{i + 1}", p))
        rows.append((f"buy_{i + 1}_vol", v))
    rows.append(("最新", 1295.01))
    rows.append(("昨收", 1290.0))
    return pd.DataFrame(rows, columns=["item", "value"])


def test_bid_ask_parses_real_akshare_shape(monkeypatch):
    import akshare

    import finresearch.tools.market as market

    monkeypatch.setattr(akshare, "stock_bid_ask_em", lambda symbol: _fake_df())
    result = market.get_bid_ask("600519.SH")

    assert result["provenance"].get("error") is None
    asks, bids = result["asks"], result["bids"]
    assert [a["label"] for a in asks] == ["卖五", "卖四", "卖三", "卖二", "卖一"]
    assert [b["label"] for b in bids] == ["买一", "买二", "买三", "买四", "买五"]
    # 价格与挂单量为数值
    assert asks[-1]["price"] == 1296.0 and asks[-1]["vol"] == 9900
    assert bids[0]["price"] == 1295.0 and bids[0]["vol"] == 1100
    # 卖侧价格应整体高于买侧
    assert min(a["price"] for a in asks) > max(b["price"] for b in bids)


def test_bid_ask_empty_df_returns_structured_failure(monkeypatch):
    import akshare

    import finresearch.tools.market as market

    monkeypatch.setattr(akshare, "stock_bid_ask_em", lambda symbol: pd.DataFrame(columns=["item", "value"]))
    result = market.get_bid_ask("600519.SH")
    assert result["asks"] == [] and result["bids"] == []
    assert result["provenance"].get("error")


def test_bid_ask_asks_pure_code_not_prefixed_symbol(monkeypatch):
    """旧实现传 sh600519 导致 secid=0.sh600519 非法请求 —— 必须传纯代码。"""
    import akshare

    import finresearch.tools.market as market

    seen = {}

    def fake(symbol):
        seen["symbol"] = symbol
        return _fake_df()

    monkeypatch.setattr(akshare, "stock_bid_ask_em", fake)
    market.get_bid_ask("600519.SH")
    assert seen["symbol"] == "600519"
