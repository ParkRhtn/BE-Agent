from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_origin(value: str) -> str:
    """'https://Example.com/' → 'https://example.com'. 경로·쿼리가 붙은 주소는 받지 않는다."""
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
        raise ValueError(f"사이트 주소는 https://example.com 처럼 적으세요: {value}")
    if parsed.query or parsed.fragment or parsed.username:
        raise ValueError(f"사이트 주소에는 경로·쿼리를 넣지 않습니다: {value}")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname.lower()}{port}"


class EmbedUpdate(BaseModel):
    enabled: bool = True
    # iframe 을 띄울 수 있는 사이트. 비워 두면 어느 사이트에서나 뜬다
    allowed_origins: list[str] = Field(default=[], max_length=20)
    daily_limit: int = Field(default=100, ge=1, le=10_000)

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        return sorted({normalize_origin(v) for v in value if v.strip()})


class EmbedRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    token: str
    enabled: bool
    allowed_origins: list[str]
    daily_limit: int
    today_runs: int = 0  # 오늘(한국 날짜) 공개 링크로 실행한 횟수
    created_at: datetime


class EmbedPublic(BaseModel):
    """공개 페이지가 그릴 정보. 로그인 없이 보이므로 이름·설명 말고는 내보내지 않는다."""

    kind: Literal["workflow"] = "workflow"
    name: str
    description: str | None
    allowed_origins: list[str]  # 공개 페이지의 frame-ancestors 에 쓴다


class EmbedRunRequest(BaseModel):
    input: str = Field(default="", max_length=2_000)  # 공개라서 길이를 좁게


class EmbedRunResult(BaseModel):
    status: Literal["done", "error"]
    output: str | None = None
    error: str | None = None
