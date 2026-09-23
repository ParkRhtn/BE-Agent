"""스트리밍 응답을 별도 작업(task)에서 만들어 전달한다.

추적 문맥(OpenTelemetry contextvars)을 여는 `with` 블록이 제너레이터의 yield 를 가로지르면
응답을 흘려보내는 쪽의 문맥과 섞일 수 있다. 생성은 작업 안에서 끝내고 결과만 대기열로 넘긴다.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

logger = logging.getLogger(__name__)

_DONE = object()


async def stream_in_task(source: Callable[[], AsyncIterator[str]]) -> AsyncIterator[str]:
    queue: asyncio.Queue[object] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for item in source():
                await queue.put(item)
        except Exception:
            logger.exception("Streaming task failed")
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(produce())
    try:
        while (item := await queue.get()) is not _DONE:
            yield item  # type: ignore[misc]
    finally:
        task.cancel()  # 브라우저가 중지하면 남은 모델 호출도 멈춘다
