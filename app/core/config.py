from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Event-Sourced Ledger API"
    app_version: str = "1.0.0"
    debug: bool = False

    # Database
    database_url: str = "sqlite+aiosqlite:///./ledger.db"
    database_echo: bool = False

    # Security
    secret_key: str = "CHANGE_ME_IN_PRODUCTION_supersecretkey12345"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Ledger invariants
    default_overdraft_limit: float = 0.0   # 0 = no overdraft allowed by default
    max_transaction_amount: float = 1_000_000.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEDGER_",
        extra="ignore",
    )

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug_value(cls, value):
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"dev", "development"}:
                return True
        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        # Managed Postgres providers (Render, Heroku, etc.) hand out plain
        # postgres(ql):// URLs, but SQLAlchemy's async engine needs the
        # +asyncpg driver suffix explicitly.
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value


@lru_cache()
def get_settings() -> Settings:
    return Settings()
