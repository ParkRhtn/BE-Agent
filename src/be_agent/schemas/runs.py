from typing import Literal

from pydantic import BaseModel, Field


class FeedbackUpdate(BaseModel):
    value: Literal[1, -1]  # 1 = 좋아요, -1 = 별로
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackRead(BaseModel):
    run_id: str
    feedback: int | None


class UsageRow(BaseModel):
    name: str
    cost: float  # USD
    tokens: int
    calls: int


class UsageDay(BaseModel):
    date: str  # YYYY-MM-DD
    cost: float
    tokens: int


class UsageRead(BaseModel):
    enabled: bool  # Langfuse 가 꺼져 있으면 false
    days: int
    total_cost: float = 0
    total_tokens: int = 0
    calls: int = 0
    by_model: list[UsageRow] = []
    by_source: list[UsageRow] = []  # 워크플로우·대화별
    daily: list[UsageDay] = []
    error: str | None = None
