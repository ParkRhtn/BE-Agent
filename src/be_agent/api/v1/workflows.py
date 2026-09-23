import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from sqlalchemy import select

from be_agent.agent.service import AgentService, AgentSpec
from be_agent.api.deps import (
    AgentServiceDep,
    CurrentUserDep,
    ModelRegistryDep,
    SessionDep,
    SessionMakerDep,
    TracingDep,
)
from be_agent.core.model_registry import ModelNotAvailable, ModelRegistry
from be_agent.core.observability import SpanHandle, SpanKind, Tracing
from be_agent.db.models import Agent, Run, User, Workflow
from be_agent.schemas.runs import RunSummary
from be_agent.schemas.workflows import (
    NodePosition,
    WorkflowCreate,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRead,
    WorkflowRunRequest,
    WorkflowUpdate,
)
from be_agent.streaming.background import stream_in_task
from be_agent.workflow.engine import RunEvent, ValidationContext, WorkflowExecutor, validate_graph

router = APIRouter(prefix="/workflows", tags=["workflows"])

# 노드 종류 → Langfuse 기록 종류
_NODE_SPAN_KIND: dict[str, SpanKind] = {"agent": "agent", "tool": "tool"}


def _default_graph() -> WorkflowGraph:
    return WorkflowGraph(
        nodes=[
            WorkflowNode(id="start", type="start", position=NodePosition(x=0, y=0)),
            WorkflowNode(id="end", type="end", position=NodePosition(x=360, y=0)),
        ],
        edges=[WorkflowEdge(id="start-end", source="start", target="end")],
    )


async def _get_workflow_or_404(session: SessionDep, workflow_id: str, user: User) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None or workflow.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "워크플로우를 찾을 수 없습니다.")
    return workflow


@router.get("", response_model=list[WorkflowRead])
async def list_workflows(session: SessionDep, user: CurrentUserDep) -> list[Workflow]:
    result = await session.scalars(
        select(Workflow).where(Workflow.user_id == user.id).order_by(Workflow.updated_at.desc())
    )
    return list(result)


@router.post("", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
async def create_workflow(body: WorkflowCreate, session: SessionDep, user: CurrentUserDep) -> Workflow:
    graph = body.graph or _default_graph()
    workflow = Workflow(user_id=user.id, name=body.name, description=body.description, graph=graph.model_dump())
    session.add(workflow)
    await session.commit()
    return workflow


@router.get("/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> Workflow:
    return await _get_workflow_or_404(session, workflow_id, user)


@router.patch("/{workflow_id}", response_model=WorkflowRead)
async def update_workflow(
    workflow_id: str, body: WorkflowUpdate, session: SessionDep, user: CurrentUserDep
) -> Workflow:
    """그래프는 미완성이어도 저장할 수 있다. 검증은 실행할 때 한다."""
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    for name, value in body.model_dump(exclude_unset=True).items():
        setattr(workflow, name, value)
    await session.commit()
    return workflow


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    await session.delete(workflow)
    await session.commit()


@dataclass
class _RunRecord:
    """실행 이벤트를 모아 실행 기록(runs)에 저장할 모양으로 만든다."""

    status: str = "running"
    output: str | None = None
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, event: RunEvent) -> None:
        match event.type:
            case "node_start":
                self.steps.append({"node_id": event.node_id, "status": "running"})
            case "node_skip":
                self.steps.append({"node_id": event.node_id, "status": "skipped"})
            case "node_finish":
                self._step(event.node_id).update(status="done", output=event.output)
            case "node_error":
                self._step(event.node_id).update(status="error", error=event.error)
                self.status, self.error = "error", f"{event.node_id}: {event.error}"
            case "run_finish":
                self.status, self.output = "done", event.output

    def _step(self, node_id: str | None) -> dict[str, Any]:
        for step in reversed(self.steps):
            if step["node_id"] == node_id:
                return step
        step = {"node_id": node_id, "status": "running"}
        self.steps.append(step)
        return step


class _ServiceRuntime:
    """AgentService 를 워크플로우 엔진의 Runtime 으로 감싼다."""

    def __init__(self, service: AgentService, registry: ModelRegistry, tracing: Tracing) -> None:
        self._service = service
        self._registry = registry
        self._tracing = tracing
        self._run_id = uuid.uuid4().hex
        self.callbacks: list[Any] = tracing.callbacks

    @contextmanager
    def trace_node(self, node_id: str, kind: str, input: Any) -> Iterator[SpanHandle]:
        with self._tracing.span(f"{node_id} ({kind})", kind=_NODE_SPAN_KIND.get(kind, "span"), input=input) as span:
            yield span

    def create_model(self, model_id: str | None) -> BaseChatModel:
        return self._service.create_model(self._registry.resolve(model_id))

    def get_tool(self, name: str) -> BaseTool | None:
        return self._service.get_tool(name)

    async def run_agent(self, spec: AgentSpec, message: str) -> str:
        return await self._service.run_once(
            spec=spec, message=message, run_id=f"{self._run_id}-{uuid.uuid4().hex}", run_name="에이전트"
        )


@router.post(
    "/{workflow_id}/run",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "노드별 실행 이벤트 (SSE, data: JSON)"},
        400: {"description": "그래프 검증 실패. detail 에 오류 목록"},
    },
)
async def run_workflow(
    workflow_id: str,
    body: WorkflowRunRequest,
    session: SessionDep,
    registry: ModelRegistryDep,
    service: AgentServiceDep,
    tracing: TracingDep,
    sessionmaker: SessionMakerDep,
    user: CurrentUserDep,
) -> StreamingResponse:
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    graph = WorkflowGraph.model_validate(workflow.graph)

    agents: dict[str, AgentSpec] = {}
    for a in await session.scalars(select(Agent).where(Agent.user_id == user.id)):
        try:
            _, config = registry.resolve_or_default(a.model)
        except ModelNotAvailable:
            continue  # 쓸 모델이 없는 에이전트는 검증에서 "에이전트를 선택하세요" 로 걸린다
        agents[a.id] = AgentSpec(model=config, system_prompt=a.system_prompt, tools=tuple(a.tools))
    ctx = ValidationContext(
        tool_names={t.name for t in service.tools},
        agent_ids=set(agents),
        allowed_models=set(registry.configs),
        has_default_model=registry.default_id is not None,
    )
    if errors := validate_graph(graph, ctx):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, errors)

    executor = WorkflowExecutor(graph, _ServiceRuntime(service, registry, tracing), agents)
    run_id = uuid.uuid4().hex
    run = Run(
        id=run_id,
        user_id=user.id,
        kind="workflow",
        workflow_id=workflow.id,
        trace_id=Tracing.trace_id_for(run_id),
        status="running",
        input=body.input,
    )
    session.add(run)
    await session.commit()
    trace_name = f"워크플로우: {workflow.name}"
    user_id, workflow_id, trace_id = user.id, workflow.id, run.trace_id

    async def save(record: _RunRecord) -> None:
        # 요청 세션은 이미 닫혔을 수 있어 새 세션으로 저장한다
        async with sessionmaker() as db:
            saved = await db.get(Run, run_id)
            if saved is not None:
                saved.status, saved.output, saved.error = record.status, record.output, record.error
                saved.steps = record.steps
                saved.finished_at = datetime.now(UTC)
                await db.commit()

    async def traced() -> AsyncIterator[str]:
        record = _RunRecord()
        try:
            # 실행 한 번 = Langfuse 기록 하나. 노드마다 하위 기록이 붙는다.
            with tracing.trace(
                trace_name,
                user_id=user_id,
                session_id=f"workflow-{workflow_id}",
                tags=["workflow"],
                input=body.input,
                trace_id=trace_id,
            ) as root:
                async for event in executor.run(body.input, run_id=run_id):
                    record.apply(event)
                    if event.type == "run_finish":
                        root.finish(output=event.output)
                    elif event.type == "node_error":
                        root.finish(error=f"{event.node_id}: {event.error}")
                    yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            record.status = "cancelled"  # 브라우저에서 중지
            raise
        except Exception as exc:
            record.status, record.error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if record.status == "running":
                record.status = "cancelled"
            # 취소 중에도 저장은 끝까지 한다
            await asyncio.shield(save(record))

    return StreamingResponse(
        stream_in_task(traced), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.get("/{workflow_id}/runs", response_model=list[RunSummary])
async def list_workflow_runs(
    workflow_id: str, session: SessionDep, user: CurrentUserDep, limit: int = Query(30, ge=1, le=100)
) -> list[Run]:
    """최근 실행 기록 (새것부터)."""
    await _get_workflow_or_404(session, workflow_id, user)
    result = await session.scalars(
        select(Run).where(Run.workflow_id == workflow_id).order_by(Run.created_at.desc()).limit(limit)
    )
    return list(result)
