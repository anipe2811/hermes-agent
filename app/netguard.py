"""SSRF-safe HTTP fetching.

Every hop (including redirects) is resolved once, every resolved address must
be globally routable, and the connection is pinned to that exact IP so a DNS
rebinding answer between "check" and "connect" can't reach an internal host.
"""
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

ALLOWED_PORTS = {80, 443, 8080, 8443}
MAX_REDIRECTS = 5

# NAT64 prefixes translate to arbitrary IPv4 — including private ranges.
_NAT64 = [ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48")]


class FetchError(Exception):
    pass


def ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Only globally routable unicast addresses. `is_global` also rejects
    CGNAT 100.64.0.0/10 (Tailscale, Alibaba metadata) which `is_private` misses."""
    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in net for net in _NAT64):
            return False
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None and not ip_allowed(embedded):
            return False
    return ip.is_global and not ip.is_multicast


async def resolve_public(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve host; reject if *any* address is non-public (mixed answers are a
    common rebinding trick)."""
    try:
        literal = ipaddress.ip_address(host)
        addrs = [literal]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise FetchError(f"cannot resolve host {host!r}") from e
        addrs = list(dict.fromkeys(ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos))
    if not addrs:
        raise FetchError(f"cannot resolve host {host!r}")
    for ip in addrs:
        if not ip_allowed(ip):
            raise FetchError("URL not allowed (only public http/https addresses)")
    return addrs


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    encoding: str
    truncated: bool


def _check_url(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("URL not allowed (only public http/https addresses)")
    if parsed.username or parsed.password:
        raise FetchError("URL not allowed (credentials in URL)")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as e:
        raise FetchError("invalid port") from e
    if port not in ALLOWED_PORTS:
        raise FetchError(f"port {port} not allowed")
    return parsed.scheme, parsed.hostname, port


async def safe_fetch(url: str, *, max_bytes: int, timeout: float = 15,
                     transport: httpx.AsyncBaseTransport | None = None) -> FetchResult:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.1)",
               "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5"}
    # trust_env=False: an HTTP(S)_PROXY would resolve the host itself and
    # silently undo the IP pinning below.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 trust_env=False, transport=transport) as client:
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, port = _check_url(url)
            ip = (await resolve_public(host, port))[0]
            ip_host = f"[{ip}]" if ip.version == 6 else str(ip)
            default_port = 443 if scheme == "https" else 80
            netloc = ip_host if port == default_port else f"{ip_host}:{port}"
            parsed = urlparse(url)
            target = parsed._replace(scheme=scheme, netloc=netloc, fragment="").geturl()
            host_header = host if port == default_port else f"{host}:{port}"
            request = client.build_request(
                "GET", target, headers={**headers, "Host": host_header},
                extensions={"sni_hostname": host} if scheme == "https" else {},
            )
            response = await client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("redirect without Location header")
                    url = urljoin(url, location)
                    continue
                body = bytearray()
                truncated = False
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= max_bytes:
                        truncated = True
                        del body[max_bytes:]
                        break
                return FetchResult(
                    url=url,
                    status=response.status_code,
                    content_type=response.headers.get("content-type", "").split(";")[0].strip().lower(),
                    body=bytes(body),
                    encoding=response.charset_encoding or "utf-8",
                    truncated=truncated,
                )
            finally:
                await response.aclose()
        raise FetchError(f"too many redirects (>{MAX_REDIRECTS})")


# ── HTML → readable text ────────────────────────────────────────────────────

_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head", "nav", "footer", "form"}
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
          "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "main", "aside"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) with scripts/styles/nav stripped and whitespace collapsed."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in lines if line)
    return " ".join(parser.title.split()), text
