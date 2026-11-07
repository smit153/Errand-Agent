"""Runtime configuration, read from environment variables only (see .env.example)."""

from __future__ import annotations

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated settings. No secret has a default value in code."""

    model_config = SettingsConfigDict(env_prefix="ERRAND_", env_file=".env", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5433, ge=1, le=65535)
    db_name: str = "errand"
    db_user: str = "errand"
    db_password: SecretStr
    test_db_name: str = "errand_test"

    timezone: str = "Asia/Kolkata"
    mock_seed: int = 42

    lock_timeout_ms: int = Field(default=2000, ge=1, le=2000)
    statement_timeout_ms: int = Field(default=5000, ge=1)
    claim_lease_seconds: int = Field(default=60, ge=1)

    def conninfo(self, dbname: str | None = None) -> str:
        """Build a libpq conninfo string. Values are quoted by psycopg's helper."""
        return make_conninfo(
            host=self.db_host,
            port=self.db_port,
            dbname=dbname or self.db_name,
            user=self.db_user,
            password=self.db_password.get_secret_value(),
        )


def load_settings() -> Settings:
    # pydantic-settings populates required fields from the environment.
    return Settings()
