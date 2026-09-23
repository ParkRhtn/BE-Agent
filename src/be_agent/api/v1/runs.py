from fastapi import APIRouter, HTTPException, Query, status

from be_agent.api.deps import CurrentUserDep, SessionDep, SettingsDep, TracingDep
from be_agent.core.usage import load_usage
from be_agent.db.models import Run
from be_agent.schemas.runs import FeedbackRead, FeedbackUpdate, UsageRead

router = APIRouter(tags=["runs"])


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
async def get_usage(settings: SettingsDep, user: CurrentUserDep, days: int = Query(30, ge=1, le=90)) -> UsageRead:
    """최근 N일 모델 사용량과 비용 (Langfuse 집계)."""
    return await load_usage(settings, user_id=user.id, days=days)
