"""审计 P0-A1：CA 证书 ASCII 化（curl_cffi 在 Windows 用 ANSI 代码页编码 CAINFO 路径）。

安装路径含 ANSI 之外的字符（「渊」∉ cp950/Big5）时，akshare 新闻接口（唯一走
curl_cffi 的行情类调用）必抛 UnicodeEncodeError。修复：certifi 证书复制到纯 ASCII
路径并设 SSL_CERT_FILE / CURL_CA_BUNDLE / REQUESTS_CA_BUNDLE 指过去。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_server  # noqa: E402

_ENV_VARS = ("SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE")


def test_ascii_cacert_path_short_circuits(monkeypatch, tmp_path):
    import certifi

    saved = {v: os.environ.get(v) for v in _ENV_VARS}
    try:
        monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "plain" / "cacert.pem"))
        run_server._ensure_ascii_cacert()
        for v in _ENV_VARS:
            assert os.environ.get(v) == saved.get(v), f"ASCII 路径不应改动 {v}"
    finally:
        for v in _ENV_VARS:
            os.environ.pop(v, None)
            if saved[v]:
                os.environ[v] = saved[v]


def test_non_ascii_cacert_path_copied_to_ascii_location(monkeypatch, tmp_path):
    import pytest

    if not str(tmp_path).isascii():
        pytest.skip("本机 tmp 路径非 ASCII，无法构造 ASCII 目标目录")
    import certifi

    # 模拟真实故障环境：证书在含「景渊」的安装路径下，AppData 可用纯 ASCII
    src_dir = tmp_path / "nonascii_景渊"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / "cacert.pem"
    src.write_text("FAKE CERT BUNDLE", encoding="utf-8")
    assert not str(src).isascii()

    ascii_dir = tmp_path / "appdata"
    ascii_dir.mkdir()

    saved = {v: os.environ.get(v) for v in _ENV_VARS}
    try:
        monkeypatch.setattr(certifi, "where", lambda: str(src))
        monkeypatch.setattr(run_server, "app_data_dir", lambda: str(ascii_dir))

        run_server._ensure_ascii_cacert()

        copied = ascii_dir / "cacert-ascii.pem"
        assert copied.is_file()
        assert copied.read_text(encoding="utf-8") == "FAKE CERT BUNDLE"
        assert str(copied).isascii()
        for v in _ENV_VARS:
            assert os.environ.get(v) == str(copied)
    finally:
        for v in _ENV_VARS:
            os.environ.pop(v, None)
            if saved[v]:
                os.environ[v] = saved[v]


def test_port_file_write_is_atomic(monkeypatch, tmp_path):
    """B3 顺带验证：port.txt 先写 tmp 再 os.replace（修双开读到半行的竞态）。"""
    port_file = tmp_path / "port.txt"
    run_server._write_port_file(str(port_file), 9527)
    assert port_file.read_text(encoding="utf-8") == "9527"
    assert not (tmp_path / "port.txt.tmp").exists(), "tmp 文件应已被 os.replace 消费"
    # 覆写走同样路径
    run_server._write_port_file(str(port_file), 10000)
    assert port_file.read_text(encoding="utf-8") == "10000"
