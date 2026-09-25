"""모델 호출 사용량(토큰·비용)을 우리 DB 에 기록하고 모은다.

- UsageCallback: 모든 모델 호출에 붙는 LangChain 콜백. 호출이 끝나면 토큰 수를 collect_usage 로 넘긴다.
- collect_usage(): 대화 한 번·워크플로우 실행 한 번 동안 호출을 모은다. 끝나면 save_usage 로 저장한다.
- load_usage(): 사용량 화면용 집계.
"""

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import ChatGeneration, LLMResult
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from be_agent.core.pricing import cost_of
from be_agent.db.models import UsageRecord
from be_agent.schemas.runs import UsageDay, UsageRead, UsageRow


@dataclass
class ModelCall:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0  # 5분 캐시
    cache_write_1h: int = 0


@dataclass
class UsageCollector:
    calls: list[ModelCall] = field(default_factory=list)


DAY_TZ = ZoneInfo("Asia/Seoul")  # 날짜별로 나눌 때 쓰는 시간대

_current: ContextVar[UsageCollector | None] = ContextVar("usage_collector", default=None)


@contextmanager
def collect_usage() -> Iterator[UsageCollector]:
    """이 안(과 여기서 만든 하위 작업)에서 일어난 모델 호출을 모은다."""
    collector = UsageCollector()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


class UsageCallback(BaseCallbackHandler):
    # 이벤트 루프에서 바로 실행해 collect_usage 의 문맥(ContextVar)을 그대로 본다
    run_inline = True

    def __init__(self) -> None:
        self._models: dict[UUID, str] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        self._models[run_id] = (metadata or {}).get("ls_model_name") or ""

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **_: Any) -> None:
        model = self._models.pop(run_id, "")
        collector = _current.get()
        if collector is None:
            return
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                usage = getattr(message, "usage_metadata", None) or {}
                details = usage.get("input_token_details") or {}
                collector.calls.append(
                    ModelCall(
                        model=model or message.response_metadata.get("model_name") or "알 수 없는 모델",
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cache_read=details.get("cache_read") or 0,
                        # langchain-anthropic 은 5분/1시간 구분이 오면 cache_creation 을 0 으로 비우고
                        # ephemeral_* 에 나눠 담는다. 구분이 없으면 cache_creation 에 담긴다 (5분 캐시).
                        cache_write=(details.get("cache_creation") or 0)
                        + (details.get("ephemeral_5m_input_tokens") or 0),
                        cache_write_1h=details.get("ephemeral_1h_input_tokens") or 0,
                    )
                )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **_: Any) -> None:
        self._models.pop(run_id, None)


@dataclass
class _Sum:
    cost: float = 0
    tokens: int = 0
    calls: int = 0
    unpriced: int = 0  # 가격을 모르는 호출 (비용에 안 들어감)

    def add(self, record: UsageRecord) -> None:
        self.tokens += record.input_tokens + record.output_tokens
        self.calls += 1
        if record.cost is None:
            self.unpriced += 1
        else:
            self.cost += record.cost

    def row(self, name: str) -> UsageRow:
        return UsageRow(name=name, cost=self.cost, tokens=self.tokens, calls=self.calls, unpriced_calls=self.unpriced)


async def save_usage(
    sessionmaker: async_sessionmaker, collector: UsageCollector, *, user_id: str, run_id: str, source: str
) -> None:
    if not collector.calls:
        return
    async with sessionmaker() as db:
        for call in collector.calls:
            db.add(
                UsageRecord(
                    user_id=user_id,
                    run_id=run_id,
                    source=source,
                    model=call.model,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cost=cost_of(
                        call.model,
                        input_tokens=call.input_tokens,
                        output_tokens=call.output_tokens,
                        cache_read=call.cache_read,
                        cache_write=call.cache_write,
                        cache_write_1h=call.cache_write_1h,
                    ),
                )
            )
        await db.commit()


async def load_usage(session: AsyncSession, *, user_id: str, days: int, now: datetime | None = None) -> UsageRead:
    """최근 N일 (오늘 포함, 한국 시간 날짜 기준)."""
    today = (now or datetime.now(UTC)).astimezone(DAY_TZ).date()
    start_day = today - timedelta(days=days - 1)
    start = datetime.combine(start_day, time.min, tzinfo=DAY_TZ)
    records = await session.scalars(
        select(UsageRecord).where(UsageRecord.user_id == user_id, UsageRecord.created_at >= start)
    )

    total = _Sum()
    by_model: dict[str, _Sum] = defaultdict(_Sum)
    by_source: dict[str, _Sum] = defaultdict(_Sum)
    by_day: dict[date, _Sum] = defaultdict(_Sum)
    for r in records:
        for bucket in (total, by_model[r.model], by_source[r.source], by_day[r.created_at.astimezone(DAY_TZ).date()]):
            bucket.add(r)

    def ranked(groups: dict[str, _Sum]) -> list[UsageRow]:
        rows = [g.row(name) for name, g in groups.items()]
        return sorted(rows, key=lambda r: (r.cost, r.tokens), reverse=True)

    days_list = [start_day + timedelta(days=i) for i in range(days)]
    return UsageRead(
        days=days,
        total_cost=total.cost,
        total_tokens=total.tokens,
        calls=total.calls,
        unpriced_calls=total.unpriced,
        by_model=ranked(by_model),
        by_source=ranked(by_source),
        daily=[UsageDay(date=d.isoformat(), cost=by_day[d].cost, tokens=by_day[d].tokens) for d in days_list],
    )
