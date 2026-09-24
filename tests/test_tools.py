import re

import pytest

from app import tools
from app.tools import execute_tool, parse_ddg_html


@pytest.mark.asyncio
@pytest.mark.parametrize("expr,expected", [
    ("2 + 2 * 10", "22"), ("(1+2)/4", "0.75"), ("-3 % 2", "1"), ("7 // 2", "3"),
])
async def test_calculate(expr, expected):
    assert await execute_tool("calculate", {"expression": expr}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("expr", [
    "2 ** 100000", "__import__('os')", "True + 1", "[1,2]", "a + 1", "(lambda: 1)()",
])
async def test_calculate_rejects(expr):
    assert (await execute_tool("calculate", {"expression": expr})).startswith("Error")


@pytest.mark.asyncio
async def test_calculate_div_zero_and_missing_arg():
    assert await execute_tool("calculate", {"expression": "1/0"}) == "Error: division by zero"
    assert "missing" in await execute_tool("calculate", {})


@pytest.mark.asyncio
async def test_current_time_uses_configured_timezone():
    out = await execute_tool("get_current_time", {})
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00 \(Asia/Kuala_Lumpur, \w+\)$", out)


@pytest.mark.asyncio
async def test_unknown_tool():
    assert await execute_tool("rm_rf", {}) == "Unknown tool: rm_rf"


@pytest.mark.asyncio
async def test_fetch_url_blocked_internal():
    out = await execute_tool("fetch_url", {"url": "http://169.254.169.254/latest/meta-data"})
    assert out.startswith("Error: URL not allowed")


DDG_PAGE = """
<div class="result results_links results_links_deep result--ad">
 <a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=x">Ad</a>
</div>
<div class="result">
 <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fq%3D1&amp;rut=abc">Example <b>A</b></a>
 <a class="result__snippet" href="#">Snippet &amp; text</a>
</div>
<div class="result">
 <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2F&amp;rut=def">Example B</a>
</div>
"""


def test_parse_ddg_returns_urls_and_skips_ads():
    assert parse_ddg_html(DDG_PAGE, 5) == [
        ("Example A", "https://example.com/a?q=1", "Snippet & text"),
        ("Example B", "https://example.org/", ""),
    ]
    assert len(parse_ddg_html(DDG_PAGE, 1)) == 1


@pytest.mark.asyncio
async def test_search_clamps_max_results(monkeypatch):
    seen = {}

    async def fake_ddg(query, n):
        seen["n"] = n
        return "ok"

    monkeypatch.setattr(tools, "_search_ddg", fake_ddg)
    await execute_tool("search_web", {"query": "x", "max_results": 10_000})
    assert seen["n"] == 10
    await execute_tool("search_web", {"query": "x", "max_results": "junk"})
    assert seen["n"] == 5
