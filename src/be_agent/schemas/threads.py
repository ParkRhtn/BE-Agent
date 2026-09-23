from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ThreadCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    agent_id: str | None = None


class ThreadUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = None


class ThreadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None
    model: str | None
    agent_id: str | None
    created_at: datetime
    updated_at: datetime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    model: str | None = None


class UIMessage(BaseModel):
    """Vercel AI SDK 의 UIMessage 와 같은 모양."""

    id: str
    role: Literal["user", "assistant"]
    parts: list[dict[str, Any]]
    metadata: dict[str, Any] | None = None  # 답변: {runId, feedback}
