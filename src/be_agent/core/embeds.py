import secrets
from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from be_agent.core.usage import DAY_TZ
from be_agent.db.models import Embed, Run


def new_embed_token() -> str:
    return secrets.token_urlsafe(24)


async def today_runs(session: AsyncSession, embed: Embed, *, now: datetime | None = None) -> int:
    """오늘(한국 날짜 0시부터) 이 공개 링크로 실행한 횟수."""
    day_start = datetime.combine((now or datetime.now(UTC)).astimezone(DAY_TZ).date(), time.min, tzinfo=DAY_TZ)
    count = await session.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.workflow_id == embed.workflow_id, Run.trigger == "embed", Run.created_at >= day_start)
    )
    return count or 0
