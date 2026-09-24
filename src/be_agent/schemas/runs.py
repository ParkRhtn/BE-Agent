from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FeedbackUpdate(BaseModel):
    value: Literal[1, -1]  # 1 = 좋아요, -1 = 별로
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackRead(BaseModel):
    run_id: str
    feedback: int | None


class RunStep(BaseModel):
    node_id: str
    status: str  # running | done | skipped | error
    output: str | None = None
    error: str | None = None


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    trigger: str | None = None  # manual | schedule
    status: str | None
    input: str | None
    output: str | None
    error: str | None
    feedback: int | None
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    steps: list[RunStep]


class UsageRow(BaseModel):
    name: str
    cost: float  # USD
    tokens: int
    calls: int
    unpriced_calls: int = 0  # 가격표에 없는 모델 호출 (비용에 안 들어감)


class UsageDay(BaseModel):
    date: str  # YYYY-MM-DD
    cost: float
    tokens: int


class UsageRead(BaseModel):
    enabled: bool = True
    days: int
    total_cost: float = 0
    total_tokens: int = 0
    calls: int = 0
    unpriced_calls: int = 0
    by_model: list[UsageRow] = []
    by_source: list[UsageRow] = []  # 워크플로우·대화별
    daily: list[UsageDay] = []
    error: str | None = None
