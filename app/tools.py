import ast
import html as _html
import ipaddress
import operator as _op
import re
import socket
import httpx
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


# ── SSRF guard: only allow public http/https URLs ────────────────────────────

def _is_safe_url(url: str) -> bool:
    """Reject non-http(s) URLs and any host that resolves to a
    private/loopback/link-local/reserved address (blocks SSRF to the VPS
    internal network and cloud metadata endpoints)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


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
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
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
            "description": "Get the current date and time",
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
            "description": "Fetch content from a URL (HTTP GET)",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch",
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
            "description": "Evaluate a mathematical expression",
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
            "description": "Search the web using DuckDuckGo (no API key needed)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool executor ────────────────────────────────────────────────────────────

async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    try:
        if name == "get_current_time":
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        elif name == "fetch_url":
            url = arguments["url"]
            if not _is_safe_url(url):
                return "Error: URL not allowed (only public http/https addresses)"
            # follow_redirects stays False so a redirect can't bounce to an internal host
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url)
                return response.text[:3000]  # limit response size

        elif name == "calculate":
            expr = arguments["expression"]
            if len(expr) > 200:
                return "Error: expression too long"
            try:
                result = _safe_eval(ast.parse(expr, mode="eval"))
            except Exception:
                return "Error: invalid or unsupported expression"
            return str(result)

        elif name == "search_web":
            query = arguments["query"]
            max_results = int(arguments.get("max_results", 5) or 5)
            headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"}
            async with httpx.AsyncClient(timeout=15, headers=headers,
                                         follow_redirects=True) as client:
                response = await client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": query},
                )
                page = response.text

            results = []
            # Each organic result: <a class="result__a" ...>Title</a> + snippet
            blocks = re.split(r'class="result__a"', page)[1:]
            for block in blocks[:max_results]:
                title_m = re.search(r">(.*?)</a>", block, re.S)
                snip_m = re.search(r'class="result__snippet[^>]*>(.*?)</a>', block, re.S)
                title = _html.unescape(re.sub(r"<.*?>", "", title_m.group(1)).strip()) if title_m else ""
                snippet = _html.unescape(re.sub(r"<.*?>", "", snip_m.group(1)).strip()) if snip_m else ""
                if title:
                    results.append(f"- {title}" + (f": {snippet}" if snippet else ""))
            return "\n".join(results) if results else "No results found."

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        return f"Tool error: {e}"
