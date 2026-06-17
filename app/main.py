from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from app.agent import run_agent
from app.config import settings


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Hermes — Task Automation Agent powered by OpenRouter",
)

_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def verify_auth(authorization: Optional[str] = Header(default=None)):
    """If HERMES_API_KEY is configured, require a matching bearer token.
    If it is empty (default), the endpoint stays open — no behaviour change."""
    if settings.hermes_api_key:
        if authorization != f"Bearer {settings.hermes_api_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")


# ── Request / Response models ────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: Optional[list[dict]] = None


class ChatResponse(BaseModel):
    response: str
    history: list[dict]
    usage: dict


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "agent": settings.app_name,
        "version": settings.app_version,
        "model": settings.openrouter_model,
        "status": "online",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_auth)])
async def chat(request: ChatRequest):
    try:
        result = await run_agent(
            user_message=request.message,
            history=request.history,
        )
        return ChatResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
