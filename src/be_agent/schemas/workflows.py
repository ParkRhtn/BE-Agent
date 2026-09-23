from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    graph: WorkflowGraph | None = None  # 없으면 시작 → 종료 기본 그래프


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    graph: WorkflowGraph | None = None


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    graph: WorkflowGraph
    created_at: datetime
    updated_at: datetime


class WorkflowRunRequest(BaseModel):
    input: str = ""
