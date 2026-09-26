from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # 이 키로 부를 수 있는 워크플로우. 없으면(null) 전부
    workflow_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    prefix: str  # sk-be-Ab12 (원문은 다시 볼 수 없다)
    workflow_ids: list[str] | None  # null 이면 모든 워크플로우
    last_used_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    key: str  # 원문. 이 응답에서만 한 번 보여 준다


class ExtRunRequest(BaseModel):
    input: str = ""
    # true 면 노드별 이벤트를 SSE 로 흘려보낸다 (화면의 실행과 같은 형식). 기본은 끝난 뒤 결과 JSON 한 번.
    stream: bool = False


class ExtRunResult(BaseModel):
    run_id: str
    status: Literal["done", "error"]
    output: str | None = None  # 종료 노드의 출력
    error: str | None = None
