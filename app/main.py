import hmac
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import AgentError, AgentTimeout, run_agent, run_agent_events
from app.config import settings
from app.sessions import SESSION_ID_RE, SessionStore, new_session_id, sanitize_history

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("hermes")

store = SessionStore(settings.session_db_path, settings.session_ttl_days)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await store.init()
    if not settings.hermes_api_key:
        if settings.allow_unauthenticated:
            log.warning("HERMES_API_KEY is empty and ALLOW_UNAUTHENTICATED=true: /chat is OPEN to anyone")
        else:
            log.error("HERMES_API_KEY is not set: /chat will return 503 until it is configured")
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Hermes — Task Automation Agent powered by OpenRouter",
    lifespan=lifespan,
    # Don't publish the API schema on the public internet unless debugging.
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.debug else None,
)

_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Access control ───────────────────────────────────────────────────────────

async def verify_auth(authorization: str | None = Header(default=None)):
    if not settings.hermes_api_key:
        if settings.allow_unauthenticated:
            return
        raise HTTPException(status_code=503, detail="Server not configured: HERMES_API_KEY is not set")
    expected = f"Bearer {settings.hermes_api_key}".encode()
    if not hmac.compare_digest((authorization or "").encode(), expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def client_ip(request: Request) -> str:
    # The container is only reachable via Cloudflare (tunnel or nginx that
    # only accepts Cloudflare IPs), so CF-Connecting-IP is set by Cloudflare.
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-real-ip")
            or (request.client.host if request.client else "unknown"))


class RateLimiter:
    """In-memory sliding window: per-IP and global requests/minute."""

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)

    def _allow(self, key: str, limit: int, now: float) -> bool:
        if limit <= 0:
            return True
        q = self.hits[key]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def check(self, ip: str) -> bool:
        now = time.monotonic()
        if len(self.hits) > 10000:  # drop idle keys so memory stays bounded
            for k in [k for k, q in self.hits.items() if not q or q[-1] <= now - 60]:
                del self.hits[k]
        return (self._allow("__global__", settings.global_rate_limit_per_minute, now)
                and self._allow(f"ip:{ip}", settings.rate_limit_per_minute, now))


limiter = RateLimiter()


async def rate_limit(request: Request):
    if not limiter.check(client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests, slow down")


# ── Request / Response models ────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=settings.max_message_chars)
    # Pass the session_id from a previous response to continue that
    # conversation (history is then loaded server-side and `history` is ignored).
    session_id: str | None = None
    # Stateless alternative: send prior turns yourself. Only user/assistant
    # text turns are kept; older turns are trimmed to the configured budget.
    history: list[dict] | None = Field(default=None, max_length=200)


class ChatResponse(BaseModel):
    response: str
    session_id: str
    history: list[dict]
    usage: dict


# ── Helpers ──────────────────────────────────────────────────────────────────

def _trim(messages: list[dict]) -> list[dict]:
    return sanitize_history(messages, settings.max_history_messages, settings.max_history_chars)


async def _prepare(req: ChatRequest) -> tuple[str, list[dict]]:
    if req.session_id is not None:
        if not SESSION_ID_RE.match(req.session_id):
            raise HTTPException(status_code=422, detail="Invalid session_id format")
        # Unknown / expired session: continue under the same id with empty history.
        return req.session_id, _trim(await store.load(req.session_id) or [])
    return new_session_id(), _trim(req.history or [])


async def _finish(session_id: str, history: list[dict], message: str, answer: str) -> list[dict]:
    updated = _trim(history + [{"role": "user", "content": message},
                               {"role": "assistant", "content": answer}])
    await store.save(session_id, updated)
    return updated


def _log_run(ip: str, session_id: str, started: float, status: int, final: dict | None = None):
    log.info(json.dumps({
        "event": "chat", "ip": ip, "session": session_id[:8], "status": status,
        "ms": int((time.monotonic() - started) * 1000),
        "iterations": (final or {}).get("iterations"),
        "tools": (final or {}).get("tools_used"),
        "usage": (final or {}).get("usage"),
    }))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"agent": settings.app_name, "version": settings.app_version, "status": "online"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse,
          dependencies=[Depends(verify_auth), Depends(rate_limit)])
async def chat(req: ChatRequest, request: Request):
    started, ip = time.monotonic(), client_ip(request)
    session_id, history = await _prepare(req)
    try:
        final = await run_agent(req.message, history)
    except AgentTimeout as e:
        _log_run(ip, session_id, started, 504)
        raise HTTPException(status_code=504, detail=str(e)) from e
    except AgentError as e:
        log.warning("upstream error: %s", e)
        _log_run(ip, session_id, started, 502)
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception:
        log.exception("unhandled error in /chat")
        _log_run(ip, session_id, started, 500)
        raise HTTPException(status_code=500, detail="Internal error") from None

    updated = await _finish(session_id, history, req.message, final["response"])
    _log_run(ip, session_id, started, 200, final)
    return ChatResponse(response=final["response"], session_id=session_id,
                        history=updated, usage=final["usage"])


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/chat/stream", dependencies=[Depends(verify_auth), Depends(rate_limit)])
async def chat_stream(req: ChatRequest, request: Request):
    """Server-Sent Events: `session`, then `tool_call` / `tool_result` as they
    happen, then `final` (or `error`). Keeps the connection alive during long
    runs so proxies don't time out."""
    started, ip = time.monotonic(), client_ip(request)
    session_id, history = await _prepare(req)

    async def events():
        yield _sse({"type": "session", "session_id": session_id})
        try:
            async for event in run_agent_events(req.message, history):
                if event["type"] == "final":
                    event["session_id"] = session_id
                    event["history"] = await _finish(session_id, history, req.message, event["response"])
                    _log_run(ip, session_id, started, 200, event)
                yield _sse(event)
        except (AgentTimeout, AgentError) as e:
            _log_run(ip, session_id, started, 504 if isinstance(e, AgentTimeout) else 502)
            yield _sse({"type": "error", "message": str(e)})
        except Exception:
            log.exception("unhandled error in /chat/stream")
            _log_run(ip, session_id, started, 500)
            yield _sse({"type": "error", "message": "Internal error"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
