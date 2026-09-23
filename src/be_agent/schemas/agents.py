from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    system_prompt: str = Field(min_length=1)
    model: str | None = None
    tools: list[str] = []


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    system_prompt: str | None = Field(default=None, min_length=1)
    model: str | None = None
    tools: list[str] | None = None


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    system_prompt: str
    model: str | None
    tools: list[str]
    created_at: datetime
    updated_at: datetime


class ToolRead(BaseModel):
    name: str  # 에이전트·노드 설정에 저장되는 값
    label: str  # 화면 이름 (한국어)
    description: str  # 화면 설명 (한국어)
    example_args: str  # 워크플로우 도구 노드 인자 예시 (JSON)
