"""
config.py — Central application configuration.
All sensitive credentials are loaded from environment variables.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── OpenAI ──────────────────────────────────────────────────────────────
    openai_api_key: str
    openai_embedding_model: str = "text-embedding-3-small"
    openai_chat_model: str = "gpt-5-mini"
    openai_embedding_dimensions: int = 1536
    azure_endpoint:str
    azure_api_key:str

    # ── Pinecone ─────────────────────────────────────────────────────────────
    pinecone_api_key: str
    pinecone_index_name: str = "neatmail-context"
    pinecone_environment: str = "us-east-1-aws"  # update for your region

    # ── Email history ────────────────────────────────────────────────────────
    history_days: int = 60          # lookback window
    max_context_vectors: int = 5    # top-k Pinecone results
    chunk_max_tokens: int = 500     # max tokens per text chunk

    # ── Gmail API ────────────────────────────────────────────────────────────
    gmail_api_base: str = "https://www.googleapis.com/gmail/v1"

    # ── Microsoft Graph API ──────────────────────────────────────────────────
    graph_api_base: str = "https://graph.microsoft.com/v1.0"

    # ── App ──────────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    app_env: str = "production"
    dashboard_api_key: str  # Secret key required for X-API-Key header


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
