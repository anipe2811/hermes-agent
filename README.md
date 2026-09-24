# Hermes Agent

Tool-calling task agent (FastAPI + OpenRouter). Tools: `get_current_time`,
`calculate`, `search_web` (Brave API or DuckDuckGo), `fetch_url` (SSRF-guarded).

## API

All `/chat*` calls need `Authorization: Bearer $HERMES_API_KEY`.

```bash
# Start a conversation
curl -s https://hermes.growgig.tech/chat \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"message": "Apa berita tech hari ni?"}'
# -> {"response": "...", "session_id": "…", "history": [...], "usage": {...}}

# Continue it: send the session_id back (history is kept server-side)
  -d '{"message": "Ringkaskan yang kedua", "session_id": "<session_id>"}'

# Streaming progress (Server-Sent Events): session, tool_call, tool_result, final | error
curl -N https://hermes.growgig.tech/chat/stream -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{"message": "..."}'
```

A stateless `history` array is still accepted when no `session_id` is sent;
only plain `user`/`assistant` text turns are kept.

| Status | Meaning |
|---|---|
| 401 | missing/wrong bearer token |
| 422 | invalid input (empty/too-long message, >200 history items, bad session_id) |
| 429 | rate limited (nginx per-IP, app per-IP + global) |
| 502 | OpenRouter error (e.g. out of credits) |
| 503 | `HERMES_API_KEY` not configured |
| 504 | run exceeded `AGENT_TIMEOUT_SECONDS` |

## Deploy (VPS)

```bash
cp .env.example .env        # fill OPENROUTER_API_KEY; HERMES_API_KEY is generated if empty
bash scripts/setup-nginx.sh # first time: nginx (Cloudflare-only + rate limit) + build
bash scripts/deploy.sh      # every update: git pull + rebuild + health check
```

Zero-open-ports alternative: create a Cloudflare Tunnel routing
`hermes.growgig.tech` → `http://hermes:8000`, set `TUNNEL_TOKEN` in `.env`,
and run `docker compose --profile tunnel up -d --build`.

Logs (one JSON line per chat with IP, tools, token usage and cost):
`docker compose logs -f hermes`

## Develop

```bash
pip install -r requirements-dev.txt
ruff check app tests && pytest -q
```

All settings are env vars — see `.env.example` and `app/config.py`.
