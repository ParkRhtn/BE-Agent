"""로그인 없이 쓰는 공개 API (다른 웹사이트에 iframe 으로 붙인 실행 화면이 부른다).

권한은 공개 링크 하나로 좁다: 그 워크플로우의 배포본만, 링크마다 1분·하루 횟수 제한.
비용과 크레딧은 링크를 만든 사람 기준이다.
"""

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from be_agent.api.deps import SessionDep, SettingsDep, WorkflowRunnerDep
from be_agent.core.embeds import today_runs
from be_agent.core.model_registry import load_registry
from be_agent.db.models import Embed, User, Workflow
from be_agent.schemas.embeds import EmbedPublic, EmbedRunRequest, EmbedRunResult
from be_agent.workflow.result import collect
from be_agent.workflow.runner import WorkflowCreditsExhausted, WorkflowInvalid, WorkflowNotPublished

router = APIRouter(prefix="/public", tags=["public"])

_NOT_FOUND = "링크를 찾을 수 없습니다."
_UNAVAILABLE = "지금은 사용할 수 없습니다. 잠시 뒤에 다시 시도하세요."


async def _live_embed(session: SessionDep, token: str) -> tuple[Embed, Workflow]:
    """켜져 있고, 워크플로우가 배포된 공개 링크만. 나머지는 모두 없는 것처럼 404."""
    embed = await session.scalar(select(Embed).where(Embed.token == token))
    workflow = await session.get(Workflow, embed.workflow_id) if embed else None
    if embed is None or not embed.enabled or workflow is None or workflow.published_graph is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    return embed, workflow


@router.get("/embeds/{token}", response_model=EmbedPublic)
async def get_embed(token: str, session: SessionDep) -> EmbedPublic:
    embed, workflow = await _live_embed(session, token)
    return EmbedPublic(name=workflow.name, description=workflow.description, allowed_origins=embed.allowed_origins)


@router.post("/embeds/{token}/run", response_model=EmbedRunResult)
async def run_embed(
    token: str,
    body: EmbedRunRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    runner: WorkflowRunnerDep,
) -> EmbedRunResult:
    embed, workflow = await _live_embed(session, token)
    if not request.app.state.embed_rate_limiter.allow(embed.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "요청이 너무 많습니다. 잠시 뒤에 다시 시도하세요.")
    if await today_runs(session, embed) >= embed.daily_limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "오늘 사용할 수 있는 횟수를 모두 썼습니다.")

    owner = await session.get(User, embed.user_id)
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND)
    registry = await load_registry(session, owner, settings)
    try:
        run_id, events = await runner.start(
            session, user=owner, workflow=workflow, registry=registry, input=body.input, trigger="embed", published=True
        )
    except WorkflowNotPublished as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_FOUND) from exc
    except (WorkflowCreditsExhausted, WorkflowInvalid) as exc:
        # 방문자에게 소유자의 크레딧·설정 문제를 자세히 보이지 않는다 (실행 기록에도 남지 않는다)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc

    result = await collect(run_id, events)
    # 노드 오류 내용은 소유자 화면의 실행 기록에서 본다. 방문자에게는 일반 문구만.
    return EmbedRunResult(
        status=result.status, output=result.output, error=None if result.status == "done" else _UNAVAILABLE
    )
