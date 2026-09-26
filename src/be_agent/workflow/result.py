"""실행 이벤트를 끝까지 받아 결과 하나로 모은다 (외부 API·공개 링크처럼 스트리밍하지 않는 호출용)."""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

from be_agent.core.errors import UserFacingError
from be_agent.workflow.engine import RunEvent

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    status: Literal["done", "error"] = "error"
    output: str | None = None
    error: str | None = None


async def collect(run_id: str, events: Callable[[], AsyncIterator[RunEvent]]) -> RunResult:
    result = RunResult()
    try:
        async for event in events():
            if event.type == "run_finish":
                result.status, result.output = "done", event.output
            elif event.type == "node_error":
                result.error = f"[{event.node_id}] {event.error}"
    except UserFacingError as exc:
        result.error = str(exc)
    except Exception:
        # 실행 기록(runs)에는 원인이 남는다. 밖으로는 내부 오류를 그대로 보이지 않는다.
        logger.exception("Workflow run failed (run %s)", run_id)
        result.error = "실행 중 오류가 발생했습니다."
    if result.status == "error" and result.error is None:
        result.error = "실행이 끝나지 않았습니다."
    return result
