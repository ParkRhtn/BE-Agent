"""크레딧: 서버 키(.env) 모델 사용분을 실측 원가 기준으로 차감한다.

- 1 크레딧 = 원가 (1 / credits_per_usd) USD. 판매 가격(마진)은 플랜에서 정한다.
- 금액은 1/1000 크레딧 정수(milli)로 저장해 합계에 오차가 없게 한다.
- 잔액 = credit_transactions 합계. 실행이 끝난 뒤 차감하므로 마지막 실행에서 잔액이 음수가 될 수 있고,
  그 다음 실행부터 막힌다.
"""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from be_agent.db.models import CreditTransaction

MILLI = 1000


def credits_to_milli(credits: float) -> int:
    return int((Decimal(str(credits)) * MILLI).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def milli_to_credits(milli: int) -> float:
    return milli / MILLI


def cost_to_milli(cost_usd: float, credits_per_usd: float) -> int:
    milli = Decimal(str(cost_usd)) * Decimal(str(credits_per_usd)) * MILLI
    return int(milli.quantize(Decimal(1), rounding=ROUND_HALF_UP))


async def balance_milli(session: AsyncSession, user_id: str) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(CreditTransaction.amount_milli), 0)).where(CreditTransaction.user_id == user_id)
    )
    return int(total or 0)
