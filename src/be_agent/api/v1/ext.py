"""외부 서비스용 간편 API. Authorization: Bearer sk-be-… (설정 → API 키에서 발급).

    curl -X POST https://<서버>/api/v1/ext/workflows/<ID>/run \
      -H "Authorization: Bearer sk-be-..." -H "Content-Type: application/json" \
      -d '{"input": "안녕하세요"}'
    → {"run_id": "...", "status": "done", "output": "...", "error": null}
"""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from be_agent.api.deps import ExtAuthDep, SessionDep, SettingsDep, WorkflowRunnerDep
from be_agent.core.model_registry import load_registry
from be_agent.db.models import Workflow
from be_agent.schemas.api_keys import ExtRunRequest, ExtRunResult
from be_agent.streaming.background import stream_in_task
from be_agent.workflow.result import collect
from be_agent.workflow.runner import WorkflowCreditsExhausted, WorkflowInvalid, WorkflowNotPublished

router = APIRouter(prefix="/ext", tags=["external"])


@router.post(
    "/workflows/{workflow_id}/run",
    response_model=ExtRunResult,
    responses={
        200: {
            "description": "stream=false: 결과 JSON / stream=true: 노드별 실행 이벤트 (SSE)",
            "content": {"text/event-stream": {}},
        },
        400: {"description": "워크플로우 검증 실패. detail 에 오류 목록"},
        401: {"description": "API 키가 없거나 잘못됨"},
        402: {"description": "서버 키 모델의 크레딧 부족"},
        404: {"description": "워크플로우가 없거나 이 키로 부를 수 없음"},
        409: {"description": "배포되지 않은 워크플로우 (배포본만 실행한다)"},
        429: {"description": "요청이 너무 많음 (키 하나당 1분 제한)"},
    },
)
async def run_workflow(
    workflow_id: str,
    body: ExtRunRequest,
    session: SessionDep,
    settings: SettingsDep,
    runner: WorkflowRunnerDep,
    auth: ExtAuthDep,
) -> ExtRunResult | StreamingResponse:
    """배포된 워크플로우를 실행한다. 기본은 끝날 때까지 기다렸다가 결과를 한 번에 돌려준다."""
    user = auth.user
    workflow = await session.get(Workflow, workflow_id)
    # 남의 것이거나 이 키에 허락되지 않은 워크플로우는 있는지조차 알리지 않는다
    if workflow is None or workflow.user_id != user.id or not auth.can_run(workflow_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "워크플로우를 찾을 수 없습니다.")
    registry = await load_registry(session, user, settings)
    try:
        run_id, events = await runner.start(
            session, user=user, workflow=workflow, registry=registry, input=body.input, trigger="api", published=True
        )
    except WorkflowNotPublished as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from exc
    except WorkflowCreditsExhausted as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, exc.errors) from exc
    except WorkflowInvalid as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.errors) from exc

    if body.stream:

        async def sse() -> AsyncIterator[str]:
            async for event in events():
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"

        return StreamingResponse(
            stream_in_task(sse), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    result = await collect(run_id, events)
    return ExtRunResult(run_id=run_id, status=result.status, output=result.output, error=result.error)
