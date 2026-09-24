from fastapi import APIRouter, HTTPException, Query, status

from be_agent.api.deps import CurrentUserDep, SessionDep, TracingDep
from be_agent.core.usage import load_usage
from be_agent.db.models import Run
from be_agent.schemas.runs import FeedbackRead, FeedbackUpdate, RunDetail, RunSummary, UsageRead

router = APIRouter(tags=["runs"])


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: str, session: SessionDep, user: CurrentUserDep) -> RunDetail:
    """실행 한 번의 입력·노드별 결과·최종 출력."""
    run = await session.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "실행 기록을 찾을 수 없습니다.")
    return RunDetail.model_validate({**RunSummary.model_validate(run).model_dump(), "steps": run.steps or []})


@router.put("/runs/{run_id}/feedback", response_model=FeedbackRead)
async def set_feedback(
    run_id: str, body: FeedbackUpdate, session: SessionDep, tracing: TracingDep, user: CurrentUserDep
) -> FeedbackRead:
    """채팅 답변·워크플로우 실행에 👍/👎. Langfuse 기록에는 user-feedback 점수로 붙는다."""
    run = await session.get(Run, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "실행 기록을 찾을 수 없습니다.")
    run.feedback = body.value
    run.feedback_comment = body.comment
    await session.commit()
    tracing.score_feedback(trace_id=run.trace_id, value=body.value, comment=body.comment)
    return FeedbackRead(run_id=run.id, feedback=run.feedback)


@router.get("/usage", response_model=UsageRead)
async def get_usage(session: SessionDep, user: CurrentUserDep, days: int = Query(30, ge=1, le=90)) -> UsageRead:
    """최근 N일 모델 사용량과 비용. 모델 호출마다 남긴 기록을 모은다."""
    return await load_usage(session, user_id=user.id, days=days)
