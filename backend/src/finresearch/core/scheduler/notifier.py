"""Bounded, opt-in delivery to configured notification channels."""

import base64
import hashlib
import hmac
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ...config import settings
from ..guardrails.net_guard import build_safe_sync_client, validate_outbound_host

_VENDORS = {
    "wework": ("qyapi.weixin.qq.com", "/cgi-bin/webhook/send"),
    "feishu": ("open.feishu.cn", "/open-apis/bot/v2/hook/"),
    "dingtalk": ("oapi.dingtalk.com", "/robot/send"),
}


def validate_webhook(channel: str, url: str) -> None:
    target = urlsplit(url)
    host, path = _VENDORS[channel]
    path_ok = target.path.startswith(path) if path.endswith("/") else target.path == path
    if target.scheme != "https" or target.hostname != host or target.port not in (None, 443) or not path_ok:
        raise ValueError("Webhook 需使用对应平台的 HTTPS 机器人地址。")
    if target.username or target.password or target.fragment:
        raise ValueError("Webhook 地址格式不正确。")


def _signed_dingtalk(url: str, secret: str) -> str:
    millis = str(int(time.time() * 1000))
    signature = base64.b64encode(hmac.digest(secret.encode(), (millis + "\n" + secret).encode(), hashlib.sha256)).decode()
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(timestamp=millis, sign=signature)
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _webhook(channel: str, cfg: dict, title: str, body: str) -> None:
    url = cfg["webhook"]
    validate_webhook(channel, url)
    message = title + "\n" + body
    if channel == "feishu":
        payload = {"msg_type": "text", "content": {"text": message}}
        if cfg.get("secret"):
            timestamp = str(int(time.time()))
            key = (timestamp + "\n" + cfg["secret"]).encode()
            payload.update(timestamp=timestamp, sign=base64.b64encode(hmac.digest(key, b"", "sha256")).decode())
    else:
        payload = {"msgtype": "text", "text": {"content": message}}
        if channel == "dingtalk" and cfg.get("secret"):
            url = _signed_dingtalk(url, cfg["secret"])
    allow_private = not settings.fin_web or settings.fin_allow_private_endpoints
    with build_safe_sync_client(
        allow_private_local=allow_private,
        self_port=settings.port,
        timeout=8,
        follow_redirects=False,
    ) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict):
        raise ValueError("通知平台响应无效。")
    code = result.get("code", result.get("StatusCode")) if channel == "feishu" else result.get("errcode")
    if code not in (0, "0"):
        raise ValueError("通知平台拒绝了消息。")


def _smtp(cfg: dict, title: str, body: str, attachment: bytes | None) -> None:
    message = EmailMessage()
    message["Subject"] = title
    message["From"] = cfg["sender"]
    message["To"] = cfg["recipient"]
    message.set_content(body)
    if attachment:
        message.add_attachment(attachment, maintype="application", subtype="pdf", filename="JingYuan-market-recap.pdf")
    context = ssl.create_default_context()
    host, port = cfg["host"], int(cfg.get("port") or 465)
    validate_outbound_host(
        host,
        port,
        allow_private_local=not settings.fin_web or settings.fin_allow_private_endpoints,
        self_port=settings.port,
        resolve_dns=True,
    )
    implicit_tls = cfg.get("tls", "ssl") == "ssl"
    client = smtplib.SMTP_SSL(host, port, timeout=10, context=context) if implicit_tls else smtplib.SMTP(host, port, timeout=10)
    with client:
        if not implicit_tls:
            client.starttls(context=context)
        if cfg.get("username") and cfg.get("password"):
            client.login(cfg["username"], cfg["password"])
        if client.send_message(message):
            raise ValueError("SMTP 服务器拒绝了部分收件人。")


def _clip_webhook_body(body: str, limit: int = 1800) -> str:
    if len(body) <= limit:
        return body
    footer = "\n内容用于事实核对和研究参考。"
    if body.endswith("内容用于事实核对和研究参考。"):
        budget = max(80, limit - len(footer) - 2)
        return body[:budget].rstrip() + "…" + footer
    return body[: limit - 1].rstrip() + "…"


def send_channel(channel: str, config: dict, title: str, body: str, attachment: bytes | None = None) -> dict:
    try:
        if channel == "smtp":
            _smtp(config, title, body, attachment)
        else:
            _webhook(channel, config, title, _clip_webhook_body(body, 1800))
    except Exception as exc:
        return {"ok": False, "error": "发送失败，请检查配置或通知平台连接。", "error_type": type(exc).__name__}
    return {"ok": True}
