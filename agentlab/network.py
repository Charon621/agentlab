"""Validate every DNS result, connect to its checked IP, retain original TLS SNI."""
import ipaddress
import socket
from urllib.parse import urlparse
import httpcore
import httpx


def validate_model_url(url, allow_fixture=False):
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
        if not host or "%" in host or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.scheme != "https" and not (allow_fixture and parsed.scheme == "http"):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if not allow_fixture:
            if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal")):
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address and not safe_address(address):
                raise ValueError
        return url.rstrip("/")
    except ValueError:
        from .providers import ProviderError
        raise ProviderError("接口地址不安全：需使用无账号、查询参数和片段的公网 HTTPS 地址。", "unsafe_url") from None


def safe_address(address):
    if address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else False:
        return safe_address(address.ipv4_mapped)
    return address.is_global and not any((address.is_multicast, address.is_reserved, address.is_unspecified, address.is_loopback, address.is_link_local, getattr(address, "is_site_local", False)))


def public_addresses(host, port):
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise httpcore.ConnectError("DNS resolution failed") from None
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not safe_address(ipaddress.ip_address(ip)) for ip in addresses):
        from .providers import ProviderError
        raise ProviderError("接口 DNS 指向非公网地址，已阻止连接。", "unsafe_url")
    return addresses


class PublicNetworkBackend(httpcore.SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        addresses = public_addresses(host, port)
        # httpcore still uses the original URL hostname for Host and TLS server_hostname.
        # The socket connects to a checked literal, preventing a second DNS lookup.
        return super().connect_tcp(addresses[0], port, timeout, local_address, socket_options)


class PublicHTTPTransport(httpx.HTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.ConnectionPool(network_backend=PublicNetworkBackend(), retries=0)
