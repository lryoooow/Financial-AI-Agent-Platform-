"""Character-level citation anchors and bounded fetching of public articles."""

import http.client
import ipaddress
import re
import socket
import ssl
import unicodedata
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

from rapidfuzz.fuzz import partial_ratio_alignment

from ..core.guardrails.untrusted import fence_external
from . import make_provenance

MAX_RESPONSE = 5 * 1024 * 1024
_FIGURES = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:万亿元|万亿|亿元|万元|元|亿|万|%|倍|个百分点|百分点))?")
_NAT64_NET = ipaddress.ip_network("64:ff9b::/96")
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_TRAILING_NUM_OR_UNIT = re.compile(r"^(?:[.,]\d|\d|\s*(?:万亿元|万亿|亿元|万元|亿|万|元|个百分点|百分点|%|倍))")


def _figures(text: str) -> tuple:
    normalized = unicodedata.normalize("NFKC", text)
    values = []
    for match in _FIGURES.finditer(normalized):
        entry = match[0].replace(",", "").replace(" ", "")
        number = re.match(r"[+-]?\d+(?:\.\d+)?", entry)
        if number:
            values.append((Decimal(number[0]), entry[number.end():].strip()))
    return tuple(values)


def _has_boundary_truncation(quote: str, source_text: str, start: int, end: int) -> bool:
    q = quote.strip()
    if not q:
        return True
    lead_ws = len(quote) - len(quote.lstrip())
    trail_ws = len(quote) - len(quote.rstrip())
    s_idx = start + lead_ws
    e_idx = end - trail_ws
    if s_idx > 0 and q[0].isdigit():
        prev_ch = source_text[s_idx - 1]
        if prev_ch.isdigit() or prev_ch in ".,+-−":
            return True
    if e_idx < len(source_text):
        rest = source_text[e_idx:]
        if q[-1].isdigit() and _TRAILING_NUM_OR_UNIT.match(rest):
            return True
        if q.endswith("万") and rest.startswith("亿"):
            return True
    return False


def verify_citation(quote: str, source_text: str) -> dict:
    unverified = {"verified": False, "score": 0.0, "start": None, "end": None}
    if not quote.strip() or not source_text.strip() or len(quote) > 2000 or len(source_text) > 240_000:
        return unverified
    pos = source_text.find(quote)
    while pos >= 0:
        end_pos = pos + len(quote)
        if not _has_boundary_truncation(quote, source_text, pos, end_pos):
            return {"verified": True, "score": 100.0, "start": pos, "end": end_pos, "method": "exact"}
        pos = source_text.find(quote, pos + 1)
    match = partial_ratio_alignment(quote, source_text, score_cutoff=85)
    if not match or match.src_end - match.src_start < len(quote) * 0.85:
        return unverified
    if _has_boundary_truncation(quote, source_text, match.dest_start, match.dest_end):
        return unverified
    if _figures(quote) != _figures(source_text[match.dest_start:match.dest_end]):
        return unverified
    return {"verified": True, "score": round(match.score, 2), "start": match.dest_start, "end": match.dest_end, "method": "fuzzy"}


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_NET:
            return False
        if ip.ipv4_mapped is not None:
            return _is_public_ip(ip.ipv4_mapped)
    else:
        if ip in _CGNAT_NET:
            return False
    return bool(ip.is_global)


def _public_target(url: str) -> tuple:
    target = urlsplit(url)
    if target.scheme not in ("http", "https") or not target.hostname or target.username is not None or target.password is not None:
        raise ValueError("只支持无凭据的公开 HTTP(S) 地址。")
    port = target.port or (443 if target.scheme == "https" else 80)
    if port not in (80, 443):
        raise ValueError("公开正文地址需使用标准 HTTP(S) 端口。")
    ips = [
        ipaddress.ip_address(address[4][0])
        for address in socket.getaddrinfo(target.hostname, port, type=socket.SOCK_STREAM)
    ]
    if not ips or any(not _is_public_ip(ip) for ip in ips):
        raise ValueError("拒绝访问非公网地址。")
    return target, port, str(ips[0])


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str):
        super().__init__(host, port, timeout=5, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.sock = self._context.wrap_socket(
            socket.create_connection((self.address, self.port), timeout=self.timeout),
            server_hostname=self.host,
        )


def _fetch_public(url: str) -> tuple[str, bytes]:
    for _ in range(4):
        target, port, address = _public_target(url)
        if target.scheme == "https":
            connection = _PinnedHTTPS(target.hostname, port, address)
        else:
            connection = http.client.HTTPConnection(address, port, timeout=5)
        try:
            path = (target.path or "/") + (f"?{target.query}" if target.query else "")
            connection.request("GET", path, headers={
                "Host": target.netloc, "Accept": "text/html,text/plain",
                "Accept-Encoding": "identity", "User-Agent": "JingYuan/2.0",
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = (response.getheader("Location") or "").strip()
                if not location:
                    raise ValueError("网页重定向缺少目标地址。")
                url = urljoin(url, location)
                continue
            mime = (response.getheader("Content-Type") or "").split(";")[0].lower()
            if response.status != 200 or mime not in ("text/html", "text/plain", "application/xhtml+xml"):
                raise ValueError("目标未返回可解析的网页正文。")
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ValueError("网页响应超过 5 MiB。")
            return url, body
        finally:
            connection.close()
    raise ValueError("网页重定向次数过多。")


def extract_article_text(url: str) -> dict:
    try:
        from trafilatura import extract
    except ImportError:
        return {"error": "网页正文提取组件未安装。", "provenance": make_provenance(source="web", endpoint="article", error="trafilatura_missing")}

    final_url, body = _fetch_public(url)
    article = extract(body, url=final_url, include_comments=False, include_tables=True) or ""
    if not article.strip():
        return {"error": "网页未提取到正文。", "provenance": make_provenance(source="web", endpoint="article")}
    text = article.strip()[:240_000]
    return {
        "text": text, "model_text": fence_external(text, urlsplit(final_url).hostname or "web", limit=1400),
        "provenance": make_provenance(source="web", endpoint="article", raw_value=final_url.split("?", 1)[0]),
    }
