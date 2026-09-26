import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from be_agent.api.deps import CurrentUserDep, ModelRegistryDep, SessionDep, WorkflowRunnerDep
from be_agent.core.embeds import new_embed_token, today_runs
from be_agent.db.models import Embed, Run, User, Workflow
from be_agent.schemas.embeds import EmbedRead, EmbedUpdate
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
from be_agent.workflow.runner import WorkflowCreditsExhausted, WorkflowInvalid

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
    changes = body.model_dump(exclude_unset=True)
    if "schedule" in changes:
        # 지금 이전 시각은 이미 지난 것으로 친다. 예약을 켜자마자 방금 지난 시각 몫이 도는 일을 막는다.
        workflow.schedule_last_at = datetime.now(UTC)
    if "delete_protected" in changes:
        changes["delete_protected"] = bool(changes["delete_protected"])
    for name, value in changes.items():
        setattr(workflow, name, value)
    await session.commit()
    return workflow


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    if workflow.delete_protected:
        raise HTTPException(status.HTTP_409_CONFLICT, "삭제 보호가 켜져 있습니다. 보호를 푼 뒤 삭제하세요.")
    await session.delete(workflow)
    await session.commit()


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
    runner: WorkflowRunnerDep,
    user: CurrentUserDep,
) -> StreamingResponse:
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    try:
        _, events = await runner.start(
            session, user=user, workflow=workflow, registry=registry, input=body.input, trigger="manual"
        )
    except WorkflowCreditsExhausted as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, exc.errors) from exc
    except WorkflowInvalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.errors) from exc

    async def sse() -> AsyncIterator[str]:
        async for event in events():
            yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"

    return StreamingResponse(stream_in_task(sse), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post(
    "/{workflow_id}/publish",
    response_model=WorkflowRead,
    responses={400: {"description": "그래프 검증 실패. detail 에 오류 목록"}},
)
async def publish_workflow(
    workflow_id: str, session: SessionDep, registry: ModelRegistryDep, runner: WorkflowRunnerDep, user: CurrentUserDep
) -> Workflow:
    """지금 저장된 편집본을 배포본으로. 외부 API·공개 링크는 배포본만 실행한다."""
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    if errors := await runner.validate(session, user=user, graph=workflow.graph, registry=registry):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, errors)
    workflow.published_graph = workflow.graph
    workflow.published_at = datetime.now(UTC)
    await session.commit()
    return workflow


@router.delete("/{workflow_id}/publish", response_model=WorkflowRead)
async def unpublish_workflow(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> Workflow:
    """배포를 내린다. 외부 API·공개 링크 호출은 바로 거부된다."""
    workflow = await _get_workflow_or_404(session, workflow_id, user)
    workflow.published_graph = None
    workflow.published_at = None
    await session.commit()
    return workflow


async def _embed_read(session: SessionDep, embed: Embed) -> EmbedRead:
    read = EmbedRead.model_validate(embed)
    read.today_runs = await today_runs(session, embed)
    return read


@router.get("/{workflow_id}/embed", response_model=EmbedRead | None)
async def get_embed(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> EmbedRead | None:
    """공개 링크 (iframe). 만든 적 없으면 null."""
    await _get_workflow_or_404(session, workflow_id, user)
    embed = await session.scalar(select(Embed).where(Embed.workflow_id == workflow_id))
    return await _embed_read(session, embed) if embed else None


@router.put("/{workflow_id}/embed", response_model=EmbedRead)
async def upsert_embed(workflow_id: str, body: EmbedUpdate, session: SessionDep, user: CurrentUserDep) -> EmbedRead:
    """공개 링크를 만들거나 설정을 바꾼다. 주소(token)는 처음 만들 때 한 번 정해진다."""
    await _get_workflow_or_404(session, workflow_id, user)
    embed = await session.scalar(select(Embed).where(Embed.workflow_id == workflow_id))
    if embed is None:
        embed = Embed(user_id=user.id, workflow_id=workflow_id, token=new_embed_token())
        session.add(embed)
    embed.enabled, embed.allowed_origins, embed.daily_limit = body.enabled, body.allowed_origins, body.daily_limit
    await session.commit()
    return await _embed_read(session, embed)


@router.delete("/{workflow_id}/embed", status_code=status.HTTP_204_NO_CONTENT)
async def delete_embed(workflow_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    """공개 링크를 없앤다. 다시 만들면 주소가 바뀐다."""
    await _get_workflow_or_404(session, workflow_id, user)
    embed = await session.scalar(select(Embed).where(Embed.workflow_id == workflow_id))
    if embed:
        await session.delete(embed)
        await session.commit()


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
