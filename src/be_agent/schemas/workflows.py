from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from be_agent.workflow.schedule import next_due

NodeType = Literal["start", "llm", "agent", "tool", "condition", "end"]


class NodePosition(BaseModel):
    x: float
    y: float


class WorkflowNode(BaseModel):
    """React Flow 노드와 같은 모양. 화면 전용 필드(selected, measured 등)는 저장하지 않는다."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    type: NodeType
    position: NodePosition
    data: dict[str, Any] = {}


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source: str
    target: str
    sourceHandle: str | None = None  # noqa: N815 — React Flow 필드명 그대로


class WorkflowGraph(BaseModel):
    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []


class WorkflowSchedule(BaseModel):
    """예약 실행. 한국 시간 기준."""

    enabled: bool = True
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["08:00"])
    weekdays: list[int] = Field(default=[0, 1, 2, 3, 4, 5, 6], min_length=1)  # 0=월 … 6=일
    input: str = Field(default="", max_length=10_000)  # 시작 노드에 넣을 입력

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, value: list[int]) -> list[int]:
        days = sorted(set(value))
        if not days or not all(0 <= d <= 6 for d in days):
            raise ValueError("요일은 0(월)~6(일) 중에서 하나 이상 고르세요.")
        return days


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    graph: WorkflowGraph | None = None  # 없으면 시작 → 종료 기본 그래프


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    graph: WorkflowGraph | None = None
    schedule: WorkflowSchedule | None = None  # null 을 보내면 예약을 지운다
    delete_protected: bool | None = None  # 삭제 보호


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    graph: WorkflowGraph
    published_graph: WorkflowGraph | None = Field(default=None, exclude=True)
    published_at: datetime | None = None  # 없으면 배포 전 (외부 API·공개 링크로 실행할 수 없다)
    schedule: WorkflowSchedule | None = None
    delete_protected: bool = False
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def has_unpublished_changes(self) -> bool:
        """배포한 뒤 편집본을 고쳤는지 (배포 전이면 False)"""
        return self.published_graph is not None and self.published_graph != self.graph

    @field_validator("delete_protected", mode="before")
    @classmethod
    def _protected(cls, value: bool | None) -> bool:
        return bool(value)

    @computed_field
    @property
    def next_run_at(self) -> datetime | None:
        """다음 예약 실행 시각"""
        return next_due(self.schedule.model_dump(), datetime.now(UTC)) if self.schedule else None


class WorkflowRunRequest(BaseModel):
    input: str = ""
