from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    app_name: str = "Hermes Agent"
    app_version: str = "1.1.0"
    debug: bool = False

    # ── Access control ──────────────────────────────────────────────────────
    # /chat requires `Authorization: Bearer <HERMES_API_KEY>`. If the key is
    # empty the endpoint refuses to serve (503) unless ALLOW_UNAUTHENTICATED
    # is explicitly set — an open endpoint burns OpenRouter credit.
    hermes_api_key: str = ""
    allow_unauthenticated: bool = False
    allowed_origins: str = "*"

    # Per-client-IP and global request budgets (requests per minute, 0 = off)
    rate_limit_per_minute: int = 20
    global_rate_limit_per_minute: int = 120

    # ── Input limits (cost + prompt-injection surface) ──────────────────────
    max_message_chars: int = 8000
    max_history_messages: int = 40
    max_history_chars: int = 60000

    # ── Agent loop ──────────────────────────────────────────────────────────
    max_iterations: int = 8
    # Whole-run deadline; keep under Cloudflare's 100s origin timeout.
    agent_timeout_seconds: float = 90
    llm_timeout_seconds: float = 45
    tool_timeout_seconds: float = 20
    timezone: str = "Asia/Kuala_Lumpur"

    # ── Sessions (server-side history) ──────────────────────────────────────
    session_db_path: str = "data/sessions.db"
    session_ttl_days: int = 30

    # ── Tools ───────────────────────────────────────────────────────────────
    # If set, search_web uses the Brave Search API instead of scraping
    # DuckDuckGo (which often bot-blocks datacenter IPs).
    brave_api_key: str = ""
    fetch_max_bytes: int = 2_000_000
    fetch_max_chars: int = 6000


settings = Settings()
