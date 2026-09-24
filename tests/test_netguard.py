import ipaddress

import httpx
import pytest

from app import netguard
from app.netguard import FetchError, html_to_text, ip_allowed, safe_fetch


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.0.0.1", "172.17.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "100.100.100.200",          # CGNAT: Tailscale / Alibaba metadata
    "0.0.0.0", "::1", "fe80::1", "fc00::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1",    # IPv4-mapped
    "64:ff9b::7f00:1",                         # NAT64 -> 127.0.0.1
    "2002:7f00:1::1",                          # 6to4 embedding 127.0.0.1
    "224.0.0.1",
])
def test_blocks_non_public(addr):
    assert not ip_allowed(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_allows_public(addr):
    assert ip_allowed(ipaddress.ip_address(addr))


def _fake_dns(mapping):
    async def resolve(host, port):
        ip = ipaddress.ip_address(mapping[host])
        if not ip_allowed(ip):
            raise FetchError("URL not allowed (only public http/https addresses)")
        return [ip]
    return resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/",
    "http://example.com:22/", "http://127.0.0.1/", "http://[::1]/",
])
async def test_rejects_bad_urls(url):
    with pytest.raises(FetchError):
        await safe_fetch(url, max_bytes=1000)


@pytest.mark.asyncio
async def test_pins_ip_follows_redirect_and_blocks_internal_hop(monkeypatch):
    monkeypatch.setattr(netguard, "resolve_public",
                        _fake_dns({"good.example": "93.184.216.34", "evil.example": "10.0.0.5"}))
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(302, headers={"location": "http://evil.example/admin"})

    with pytest.raises(FetchError):
        await safe_fetch("https://good.example/start", max_bytes=1000,
                         transport=httpx.MockTransport(handler))
    # Connected to the resolved IP, not the hostname, with Host + SNI preserved.
    assert seen == [("https://93.184.216.34/start", "good.example", "good.example")]


@pytest.mark.asyncio
async def test_follows_public_redirect_and_caps_body(monkeypatch):
    monkeypatch.setattr(netguard, "resolve_public",
                        _fake_dns({"a.example": "93.184.216.34", "b.example": "93.184.216.35"}))

    def handler(request: httpx.Request):
        if request.headers["host"] == "a.example":
            return httpx.Response(301, headers={"location": "https://b.example/page"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              content=b"x" * 5000)

    res = await safe_fetch("http://a.example/", max_bytes=1000, transport=httpx.MockTransport(handler))
    assert res.url == "https://b.example/page"
    assert res.status == 200 and res.content_type == "text/html"
    assert len(res.body) == 1000 and res.truncated


@pytest.mark.asyncio
async def test_too_many_redirects(monkeypatch):
    monkeypatch.setattr(netguard, "resolve_public", _fake_dns({"loop.example": "93.184.216.34"}))
    transport = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(FetchError, match="too many redirects"):
        await safe_fetch("http://loop.example/", max_bytes=100, transport=transport)


def test_html_to_text_strips_noise():
    html = """<html><head><title> My  Page </title><style>.a{}</style>
    <script>alert(1)</script></head><body><nav>Menu</nav>
    <h1>Hello</h1><p>World &amp; more</p><footer>foot</footer></body></html>"""
    title, text = html_to_text(html)
    assert title == "My Page"
    assert text == "Hello\nWorld & more"
