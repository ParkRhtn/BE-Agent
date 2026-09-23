import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from sqlalchemy import select

from be_agent.agent.service import AgentService, AgentSpec
from be_agent.api.deps import AgentServiceDep, CurrentUserDep, ModelRegistryDep, SessionDep
from be_agent.core.model_registry import ModelNotAvailable, ModelRegistry
from be_agent.db.models import Agent, User, Workflow
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
from be_agent.workflow.engine import ValidationContext, WorkflowExecutor, validate_graph

router = APIRouter(prefix="/workflows", tags=["workflows"])


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
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(workflow, field, value)
    await session.commit()
    return workflow


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    await session.delete(workflow)
    await session.commit()


class _ServiceRuntime:
    """AgentService 를 워크플로우 엔진의 Runtime 으로 감싼다."""

    def __init__(self, service: AgentService, registry: ModelRegistry) -> None:
        self._service = service
        self._registry = registry
        self._run_id = uuid.uuid4().hex

    def create_model(self, model_id: str | None) -> BaseChatModel:
        return self._service.create_model(self._registry.resolve(model_id))

    def get_tool(self, name: str) -> BaseTool | None:
        return self._service.get_tool(name)

    async def run_agent(self, spec: AgentSpec, message: str) -> str:
        return await self._service.run_once(spec=spec, message=message, run_id=f"{self._run_id}-{uuid.uuid4().hex}")


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

    executor = WorkflowExecutor(graph, _ServiceRuntime(service, registry), agents)

    async def events() -> AsyncIterator[str]:
        async for event in executor.run(body.input):
            yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
