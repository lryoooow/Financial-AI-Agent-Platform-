"""Unified outbound URL, host, and pinned-IP transport validation against SSRF & DNS rebinding."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_NAT64_NET = ipaddress.ip_network("64:ff9b::/96")
_RFC2606_TEST_TLDS = (".test", ".invalid", ".example")
_RFC2606_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_rfc2606_reserved(clean_host: str) -> bool:
    if clean_host.endswith(_RFC2606_TEST_TLDS):
        return True
    return clean_host in _RFC2606_EXAMPLE_DOMAINS or any(
        clean_host.endswith("." + dom) for dom in _RFC2606_EXAMPLE_DOMAINS
    )


def _unwrap_ip(raw_ip: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addr = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [addr]
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            out.append(addr.ipv4_mapped)
        elif addr in _NAT64_NET:
            out.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
        elif addr.sixtofour is not None:
            out.append(addr.sixtofour)
    return out


def check_ip_allowed(
    raw_ip: str,
    *,
    allow_private_local: bool,
    port: int | None = None,
    self_port: int | None = None,
) -> None:
    for ip in _unwrap_ip(raw_ip):
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError("目标地址属于受限网段，已拒绝访问")
        if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
            raise ValueError("目标地址属于受限网段，已拒绝访问")
        if ip.is_loopback and self_port and port and port == self_port:
            raise ValueError("禁止将本软件自身服务端口设为外部端点")
        if not allow_private_local and (ip.is_private or ip.is_loopback or ip.is_reserved):
            raise ValueError("当前 Web 部署禁止连接内网或本机回环端点")


def resolve_and_validate_ips(
    host: str,
    port: int,
    *,
    allow_private_local: bool,
    self_port: int | None = None,
) -> list[str]:
    clean_host = (host or "").strip().rstrip(".").lower()
    if not clean_host:
        raise ValueError("缺少有效主机名")
    if not allow_private_local and (
        clean_host in _LOCAL_HOSTNAMES
        or clean_host.endswith((".local", ".internal", ".localhost"))
    ):
        raise ValueError("当前 Web 部署禁止连接内网或本机回环端点")

    try:
        check_ip_allowed(clean_host, allow_private_local=allow_private_local, port=port, self_port=self_port)
        return [clean_host.split("%", 1)[0]]
    except ValueError as exc:
        msg = str(exc)
        if "受限网段" in msg or "自身服务端口" in msg or "禁止连接内网" in msg:
            raise

    if clean_host in _LOCAL_HOSTNAMES and self_port and port == self_port:
        raise ValueError("禁止将本软件自身服务端口设为外部端点")

    try:
        infos = socket.getaddrinfo(clean_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"无法解析主机名：{host}") from exc

    validated: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            ip_str = str(sockaddr[0])
            check_ip_allowed(
                ip_str,
                allow_private_local=allow_private_local,
                port=port,
                self_port=self_port,
            )
            if ip_str not in validated:
                validated.append(ip_str)
    if not validated:
        raise ValueError(f"无法解析主机名：{host}")
    return validated


def validate_outbound_host(
    host: str,
    port: int,
    *,
    allow_private_local: bool,
    self_port: int | None = None,
    resolve_dns: bool = True,
) -> None:
    clean_host = (host or "").strip().rstrip(".").lower()
    if not clean_host:
        raise ValueError("缺少有效主机名")
    if not allow_private_local and (
        clean_host in _LOCAL_HOSTNAMES
        or clean_host.endswith((".local", ".internal", ".localhost"))
    ):
        raise ValueError("当前 Web 部署禁止连接内网或本机回环端点")

    try:
        check_ip_allowed(clean_host, allow_private_local=allow_private_local, port=port, self_port=self_port)
        return
    except ValueError as exc:
        msg = str(exc)
        if "受限网段" in msg or "自身服务端口" in msg or "禁止连接内网" in msg:
            raise

    if clean_host in _LOCAL_HOSTNAMES and self_port and port == self_port:
        raise ValueError("禁止将本软件自身服务端口设为外部端点")

    if not resolve_dns or _is_rfc2606_reserved(clean_host):
        return

    resolve_and_validate_ips(
        clean_host,
        port,
        allow_private_local=allow_private_local,
        self_port=self_port,
    )


def validate_outbound_url(
    raw_url: str,
    *,
    allow_private_local: bool,
    self_port: int | None = None,
    resolve_dns: bool = True,
    forbid_query_fragment: bool = False,
) -> str:
    u = (raw_url or "").strip()
    if not u:
        raise ValueError("端点地址不能为空")
    parsed = urlparse(u)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError("仅支持 http:// 或 https:// 协议")
    if not parsed.hostname:
        raise ValueError("缺少有效主机名")
    if parsed.username or parsed.password:
        raise ValueError("不允许内嵌用户名或密码")
    if forbid_query_fragment and (parsed.query or parsed.fragment):
        raise ValueError("不允许包含查询参数或 # 锚点")
    elif parsed.fragment:
        raise ValueError("不允许包含 # 锚点")
    port = parsed.port or (443 if scheme == "https" else 80)
    validate_outbound_host(
        parsed.hostname,
        port,
        allow_private_local=allow_private_local,
        self_port=self_port,
        resolve_dns=resolve_dns,
    )
    return u.rstrip("/")


class _PinnedSafeAsyncBackend:
    """Resolve DNS once per TCP connect, validate every IP, and connect directly to the pinned IP."""

    def __init__(self, inner: Any, *, allow_private_local: bool, self_port: int | None) -> None:
        self._inner = inner
        self._allow_private_local = allow_private_local
        self._self_port = self_port

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        try:
            ips = await asyncio.to_thread(
                resolve_and_validate_ips,
                host,
                port,
                allow_private_local=self._allow_private_local,
                self_port=self._self_port,
            )
        except ValueError as exc:
            raise httpx.ConnectError(str(exc)) from exc

        last_exc: Exception | None = None
        for ip_addr in ips:
            try:
                return await self._inner.connect_tcp(
                    ip_addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _PinnedSafeSyncBackend:
    """Sync counterpart for httpx.Client connections."""

    def __init__(self, inner: Any, *, allow_private_local: bool, self_port: int | None) -> None:
        self._inner = inner
        self._allow_private_local = allow_private_local
        self._self_port = self_port

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        try:
            ips = resolve_and_validate_ips(
                host,
                port,
                allow_private_local=self._allow_private_local,
                self_port=self._self_port,
            )
        except ValueError as exc:
            raise httpx.ConnectError(str(exc)) from exc

        last_exc: Exception | None = None
        for ip_addr in ips:
            try:
                return self._inner.connect_tcp(
                    ip_addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _attach_safe_backends(client: Any, wrapper_cls: type, *, allow_private_local: bool, self_port: int | None) -> None:
    direct = getattr(client, "_transport", None)
    pool = getattr(direct, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if pool is not None and backend is not None and not isinstance(backend, wrapper_cls):
        pool._network_backend = wrapper_cls(
            backend, allow_private_local=allow_private_local, self_port=self_port
        )
    # Operator-configured HTTP(S)_PROXY mounts may reside on localhost/LAN; still guard metadata & self_port.
    for proxy_transport in (getattr(client, "_mounts", {}) or {}).values():
        p_pool = getattr(proxy_transport, "_pool", None)
        p_backend = getattr(p_pool, "_network_backend", None)
        if p_pool is not None and p_backend is not None and not isinstance(p_backend, wrapper_cls):
            p_pool._network_backend = wrapper_cls(
                p_backend, allow_private_local=True, self_port=self_port
            )


def secure_httpx_client(
    client: Any, *, allow_private_local: bool, self_port: int | None = None
) -> Any:
    if client is None:
        return None
    wrapper_cls = _PinnedSafeAsyncBackend if isinstance(client, httpx.AsyncClient) else _PinnedSafeSyncBackend
    _attach_safe_backends(client, wrapper_cls, allow_private_local=allow_private_local, self_port=self_port)
    return client


def build_safe_async_client(
    *, allow_private_local: bool, self_port: int | None = None, **kwargs: Any
) -> httpx.AsyncClient:
    client = httpx.AsyncClient(**kwargs)
    return secure_httpx_client(client, allow_private_local=allow_private_local, self_port=self_port)


def build_safe_sync_client(
    *, allow_private_local: bool, self_port: int | None = None, **kwargs: Any
) -> httpx.Client:
    client = httpx.Client(**kwargs)
    return secure_httpx_client(client, allow_private_local=allow_private_local, self_port=self_port)
