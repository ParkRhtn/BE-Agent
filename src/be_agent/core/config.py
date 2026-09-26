from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
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

    # MCP 도구 서버 설정 (git 에 올린다). 키는 파일 안에 ${이름} 으로 쓰고 .env 에 둔다. 끄려면 빈 값.
    mcp_config_path: Path | None = Path("mcp_servers.json")
    # 워크플로우 예약 실행. 서버를 여러 개 띄우면 하나만 켜도 되지만, 모두 켜도 같은 시각에 두 번 돌지는 않는다.
    scheduler_enabled: bool = True

    # 설정 화면에서 제공사를 등록하지 않았을 때 쓰는 키 (.env). 화면에서 등록한 키가 우선이다.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # 외부 API (API 키로 부르는 /api/v1/ext/*). 키 하나당 1분에 이만큼까지. 서버 프로세스마다 따로 센다.
    ext_rate_limit_per_minute: int = 60
    # 공개 링크(iframe). 누구나 부를 수 있으므로 링크 하나당 1분에 이만큼까지 (하루 한도는 링크마다 따로 정한다)
    embed_rate_limit_per_minute: int = 20

    # 크레딧. 위의 서버 키(.env)로 부른 모델만 실측 원가만큼 차감한다. 사용자가 등록한 키는 차감하지 않는다.
    # false 면 차감 기록은 남기되 잔액이 없어도 막지 않는다 (로컬 개발용). 공개 운영에서는 반드시 true.
    credits_enforced: bool = False
    # 원가 1 USD 가 몇 크레딧인가. 500 이면 1 크레딧 = 원가 $0.002. 판매 마진은 플랜 가격에서 붙인다.
    credits_per_usd: float = 500
    signup_credits: float = 0  # 가입할 때 주는 체험 크레딧
    admin_emails: list[str] = []  # 크레딧 충전·조정 API 를 쓸 수 있는 계정
    # 제공사 API 키 암호화용. 없으면 JWT_SECRET 에서 만든다 (그 경우 JWT_SECRET 을 바꾸면 저장된 키를 못 읽는다).
    encryption_key: str | None = None

    # 인증. 운영에서는 반드시 `openssl rand -hex 32` 등으로 만든 값으로 바꾼다.
    jwt_secret: str = _DEV_JWT_SECRET
    jwt_expire_minutes: int = 60 * 24 * 7
    allow_signup: bool = True

    # 비밀번호 재설정 메일. smtp_host 가 없으면 메일 대신 서버 로그에 링크를 출력한다 (로컬 개발용).
    frontend_url: str = "http://localhost:3000"
    password_reset_expire_minutes: int = 30
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None  # 없으면 smtp_username
    smtp_starttls: bool = True

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # Langfuse 문서의 이름(LANGFUSE_BASE_URL)과 예전 이름(LANGFUSE_HOST) 모두 받는다
    langfuse_host: str | None = Field(default=None, validation_alias=AliasChoices("langfuse_base_url", "langfuse_host"))

    @model_validator(mode="after")
    def _require_jwt_secret(self) -> "Settings":
        if not self.jwt_secret:  # .env 의 JWT_SECRET= (빈 값)
            self.jwt_secret = _DEV_JWT_SECRET
        if self.environment != "local" and self.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError("local 이 아닌 환경에서는 JWT_SECRET 을 반드시 설정해야 합니다.")
        return self

    @property
    def secret_box_key(self) -> str:
        return self.encryption_key or self.jwt_secret

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
