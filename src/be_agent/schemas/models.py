from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ProviderKindIn = Literal["anthropic", "openai", "openai_compatible"]


class ModelOption(BaseModel):
    id: str  # 에이전트·스레드·워크플로우에 저장되는 값
    label: str
    provider: str  # 제공사 표시 이름
    billing: Literal["platform", "user", "free"]  # platform 이면 크레딧 차감
    blocked_reason: str | None = None  # 지금 쓸 수 없는 이유 (크레딧 부족 등)


class ModelsRead(BaseModel):
    default: str | None
    options: list[ModelOption]


class DefaultModelUpdate(BaseModel):
    model: str


class ProviderModelRead(BaseModel):
    id: str
    label: str


class ProviderCreate(BaseModel):
    kind: ProviderKindIn
    name: str | None = Field(default=None, max_length=100)
    api_key: str | None = Field(default=None, max_length=500)
    base_url: str | None = Field(default=None, max_length=500)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: str | None = Field(default=None, min_length=1, max_length=500)
    base_url: str | None = Field(default=None, max_length=500)
    enabled_models: list[str] | None = None


class ProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    name: str
    base_url: str | None
    api_key_hint: str | None
    available_models: list[ProviderModelRead]
    enabled_models: list[str]
    verified_at: datetime | None
    created_at: datetime


class ModelTestRequest(BaseModel):
    model: str


class ModelTestResult(BaseModel):
    ok: bool
    reply: str | None = None
    error: str | None = None
    latency_ms: int | None = None
