"""Conversation history: sanitization + server-side persistence (SQLite).

Only plain user/assistant turns are kept. Client-supplied `system` or `tool`
messages (or assistant tool_calls) would let a caller forge instructions or
tool results, so they are dropped.
"""
import asyncio
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

ALLOWED_ROLES = {"user", "assistant"}
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def sanitize_history(raw: list | None, max_messages: int, max_chars: int) -> list[dict]:
    out: list[dict] = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role not in ALLOWED_ROLES or not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "content": content})
    out = out[-max_messages:] if max_messages > 0 else []
    total = sum(len(m["content"]) for m in out)
    while out and total > max_chars:  # drop oldest turns first
        total -= len(out.pop(0)["content"])
    return out


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


class SessionStore:
    def __init__(self, path: str, ttl_days: int):
        self.path = path
        self.ttl_seconds = ttl_days * 86400
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_sync(self):
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            if self.ttl_seconds > 0:
                conn.execute("DELETE FROM sessions WHERE updated_at < ?",
                             (time.time() - self.ttl_seconds,))

    def _load_sync(self, session_id: str) -> list[dict] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT messages, updated_at FROM sessions WHERE id = ?",
                               (session_id,)).fetchone()
        if not row:
            return None
        if self.ttl_seconds > 0 and row[1] < time.time() - self.ttl_seconds:
            return None
        return json.loads(row[0])

    def _save_sync(self, session_id: str, messages: list[dict]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, messages, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, "
                "updated_at = excluded.updated_at",
                (session_id, json.dumps(messages, ensure_ascii=False), time.time()),
            )

    async def init(self):
        await asyncio.to_thread(self._init_sync)

    async def load(self, session_id: str) -> list[dict] | None:
        return await asyncio.to_thread(self._load_sync, session_id)

    async def save(self, session_id: str, messages: list[dict]):
        async with self._lock:
            await asyncio.to_thread(self._save_sync, session_id, messages)
