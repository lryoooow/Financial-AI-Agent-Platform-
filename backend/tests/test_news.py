"""审计 P0-A1：新闻直连 Referer 头编码。

HTTP 头只允许 latin-1；旧实现把中文关键词裸拼进 Referer，行业研究（固定中文行业名）
直连路径 100% UnicodeEncodeError。修复后必须 percent-encode。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


def _jsonp_payload(keyword: str) -> str:
    import json

    body = {
        "result": {
            "cmsArticleWebOld": [
                {
                    "title": f"关于<em>{keyword}</em>的新闻",
                    "content": "正文摘要",
                    "date": "2026-08-28 09:00:00",
                    "url": "https://finance.eastmoney.com/a/1.html",
                    "mediaName": "东方财富",
                    "code": "1",
                }
            ]
        }
    }
    return f"jQuery_finresearch({json.dumps(body, ensure_ascii=False)});"


def test_news_direct_referer_is_latin1_safe_for_cjk_keyword(monkeypatch):
    import requests

    import finresearch.tools.news as news

    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return _FakeResp(_jsonp_payload("光伏"))

    monkeypatch.setattr(requests, "get", fake_get)
    items = news._news_direct("光伏", 5)

    referer = captured["headers"]["Referer"]
    # 头值必须能按 latin-1 编码（HTTP 头的硬约束）
    referer.encode("latin-1")
    # 中文关键词必须被 percent-encode
    assert "%E5%85%89%E4%BC%8F" in referer or "%e5%85%89" in referer.lower()
    assert "光伏" not in referer
    # 解析链路不受影响
    assert items and items[0]["title"] == "关于光伏的新闻"
    assert items[0]["source"] == "东方财富"


def test_news_direct_referer_ascii_keyword_unchanged(monkeypatch):
    import requests

    import finresearch.tools.news as news

    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return _FakeResp(_jsonp_payload("600519"))

    monkeypatch.setattr(requests, "get", fake_get)
    news._news_direct("600519", 5)
    assert "keyword=600519" in captured["headers"]["Referer"]
