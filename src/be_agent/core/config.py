from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "BE-Agent"
    environment: str = "local"
    cors_origins: list[str] = ["http://localhost:3000"]

    # sqlite+aiosqlite:///... (로컬) 또는 postgresql+asyncpg://... (운영)
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    # DB가 SQLite일 때만 사용. Postgres면 LangGraph 체크포인트도 같은 DB에 저장된다.
    checkpoint_sqlite_path: str = "./data/checkpoints.db"

    # "<provider>:<model>" 형식. fake:echo 는 API 키 없이 동작하는 개발용 모델.
    default_model: str = "fake:echo"
    allowed_models: list[str] = [
        "fake:echo",
        "anthropic:claude-sonnet-5",
        "anthropic:claude-haiku-4-5-20251001",
        "openai:gpt-5",
    ]
    system_prompt: str = "You are a helpful assistant. Answer in the user's language."

    mcp_config_path: Path | None = None

    # 인증. 운영에서는 반드시 `openssl rand -hex 32` 등으로 만든 값으로 바꾼다.
    jwt_secret: str = _DEV_JWT_SECRET
    jwt_expire_minutes: int = 60 * 24 * 7
    allow_signup: bool = True

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    @model_validator(mode="after")
    def _require_jwt_secret(self) -> "Settings":
        if not self.jwt_secret:  # .env 의 JWT_SECRET= (빈 값)
            self.jwt_secret = _DEV_JWT_SECRET
        if self.environment != "local" and self.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError("local 이 아닌 환경에서는 JWT_SECRET 을 반드시 설정해야 합니다.")
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def postgres_conninfo(self) -> str:
        """psycopg용 접속 문자열 (LangGraph Postgres 체크포인터에서 사용)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
