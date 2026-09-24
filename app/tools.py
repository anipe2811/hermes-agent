import ast
import html as _html
import json
import operator as _op
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.netguard import FetchError, html_to_text, safe_fetch

# ── Safe math evaluator (no eval, no ** to prevent CPU/memory DoS) ───────────

_ALLOWED_OPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.Mod: _op.mod,
    ast.FloorDiv: _op.floordiv,
    ast.USub: _op.neg,
    ast.UAdd: _op.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


# ── Tool definitions (sent to LLM) ──────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time in the user's timezone (ISO 8601 with UTC offset)",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a public web page (HTTP GET) and return its readable text content",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The http(s) URL to fetch",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate an arithmetic expression (+ - * / // %)",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math expression to evaluate, e.g. '2 + 2 * 10'",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web. Returns title, URL and snippet per result; "
                           "use fetch_url on a result URL to read the page",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (1-10, default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool implementations ─────────────────────────────────────────────────────

def _current_time() -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    return f"{now.isoformat(timespec='seconds')} ({settings.timezone}, {now:%A})"


async def _fetch_url(url: str) -> str:
    try:
        res = await safe_fetch(url, max_bytes=settings.fetch_max_bytes)
    except FetchError as e:
        return f"Error: {e}"
    except httpx.HTTPError as e:
        return f"Error: request failed ({type(e).__name__})"

    header = f"URL: {res.url}\nStatus: {res.status}"
    ctype = res.content_type
    raw = res.body.decode(res.encoding, errors="replace")
    if ctype in ("text/html", "application/xhtml+xml") or (not ctype and "<html" in raw[:1000].lower()):
        title, text = html_to_text(raw)
        if title:
            header += f"\nTitle: {title}"
    elif ctype.startswith("text/") or ctype in ("application/json", "application/xml", ""):
        text = raw
    else:
        return f"{header}\nError: unsupported content type {ctype!r}"

    limit = settings.fetch_max_chars
    if len(text) > limit:
        text = text[:limit] + f"\n[... truncated, {len(text) - limit} more chars]"
    return f"{header}\n\n{text}"


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<.*?>", "", s, flags=re.S)).strip()


def _format_results(results: list[tuple[str, str, str]]) -> str:
    if not results:
        return "No results found."
    return "\n".join(
        f"{i}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else "")
        for i, (title, url, snippet) in enumerate(results, 1)
    )


async def _search_brave(query: str, n: int) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": n},
            headers={"Accept": "application/json", "X-Subscription-Token": settings.brave_api_key},
        )
    if r.status_code != 200:
        return f"Error: search API returned HTTP {r.status_code}"
    items = r.json().get("web", {}).get("results", [])[:n]
    return _format_results([
        (_strip_tags(it.get("title", "")), it.get("url", ""), _strip_tags(it.get("description", "")))
        for it in items
    ])


def _ddg_target(href: str) -> str | None:
    """DDG wraps result links as //duckduckgo.com/l/?uddg=<real url>; ads go via y.js."""
    href = _html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        if parsed.path.startswith("/y.js"):
            return None
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return target
    return href or None


def parse_ddg_html(page: str, n: int) -> list[tuple[str, str, str]]:
    results = []
    for block in re.split(r'class="result__a"', page)[1:]:
        href_m = re.search(r'href="([^"]+)"', block)
        title_m = re.search(r">(.*?)</a>", block, re.S)
        snip_m = re.search(r'class="result__snippet[^>]*>(.*?)</a>', block, re.S)
        url = _ddg_target(href_m.group(1)) if href_m else None
        title = _strip_tags(title_m.group(1)) if title_m else ""
        if not url or not title:
            continue
        results.append((title, url, _strip_tags(snip_m.group(1)) if snip_m else ""))
        if len(results) >= n:
            break
    return results


async def _search_ddg(query: str, n: int) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.1)"}
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
        r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
    page = r.text
    if r.status_code == 202 or "anomaly-modal" in page or "anomaly.js" in page:
        return ("Error: DuckDuckGo blocked the search (bot check). "
                "Set BRAVE_API_KEY to use the Brave Search API instead.")
    if r.status_code != 200:
        return f"Error: search returned HTTP {r.status_code}"
    return _format_results(parse_ddg_html(page, n))


async def _search_web(query: str, max_results: Any) -> str:
    try:
        n = int(max_results) if max_results is not None else 5
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 10))
    if settings.brave_api_key:
        return await _search_brave(query, n)
    return await _search_ddg(query, n)


# ── Tool executor ────────────────────────────────────────────────────────────

def _require_str(arguments: dict[str, Any], key: str) -> str:
    val = arguments.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"missing or invalid argument {key!r}")
    return val.strip()


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Never raises — errors are returned as text so the model can react."""
    try:
        if name == "get_current_time":
            return _current_time()

        elif name == "fetch_url":
            return await _fetch_url(_require_str(arguments, "url"))

        elif name == "calculate":
            expr = _require_str(arguments, "expression")
            if len(expr) > 200:
                return "Error: expression too long"
            try:
                result = _safe_eval(ast.parse(expr, mode="eval"))
            except ZeroDivisionError:
                return "Error: division by zero"
            except Exception:
                return "Error: invalid or unsupported expression"
            return str(result)

        elif name == "search_web":
            return await _search_web(_require_str(arguments, "query"), arguments.get("max_results"))

        else:
            return f"Unknown tool: {name}"

    except (json.JSONDecodeError, KeyError) as e:  # JSONDecodeError subclasses ValueError
        return f"Tool error: unexpected response ({type(e).__name__})"
    except ValueError as e:
        return f"Error: {e}"
    except httpx.HTTPError as e:
        return f"Tool error: request failed ({type(e).__name__})"
    except Exception as e:
        return f"Tool error: {type(e).__name__}"
