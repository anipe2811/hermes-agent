from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openrouter_api_key: str
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    app_name: str = "Hermes Agent"
    app_version: str = "1.0.0"
    debug: bool = False

    # Optional hardening (both default to current behaviour):
    #   hermes_api_key — if set, /chat requires `Authorization: Bearer <key>`
    #   allowed_origins — comma-separated CORS origins ("*" = allow all)
    hermes_api_key: str = ""
    allowed_origins: str = "*"

    class Config:
        env_file = ".env"


settings = Settings()
