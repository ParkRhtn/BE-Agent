from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from be_agent.api.deps import AdminUserDep, CurrentUserDep, SessionDep, SettingsDep
from be_agent.core.config import Settings
from be_agent.core.credits import balance_milli, credits_to_milli, milli_to_credits
from be_agent.db.models import CreditTransaction, User
from be_agent.schemas.credits import CreditGrant, CreditsRead, CreditTransactionRead

router = APIRouter(tags=["credits"])


async def _credits_of(session: AsyncSession, settings: Settings, user_id: str, limit: int) -> CreditsRead:
    transactions = await session.scalars(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit)
    )
    return CreditsRead(
        balance=milli_to_credits(await balance_milli(session, user_id)),
        enforced=settings.credits_enforced,
        credits_per_usd=settings.credits_per_usd,
        transactions=[
            CreditTransactionRead(
                id=t.id,
                kind=t.kind,  # type: ignore[arg-type]
                amount=milli_to_credits(t.amount_milli),
                run_id=t.run_id,
                note=t.note,
                created_at=t.created_at,
            )
            for t in transactions
        ],
    )


async def _user_by_email(session: AsyncSession, email: str) -> User:
    user = await session.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "해당 이메일의 사용자가 없습니다.")
    return user


@router.get("/credits", response_model=CreditsRead)
async def my_credits(
    session: SessionDep, settings: SettingsDep, user: CurrentUserDep, limit: int = Query(50, ge=1, le=500)
) -> CreditsRead:
    """내 크레딧 잔액과 최근 충전·사용 내역."""
    return await _credits_of(session, settings, user.id, limit)


@router.get("/admin/credits", response_model=CreditsRead)
async def user_credits(
    session: SessionDep, settings: SettingsDep, _: AdminUserDep, email: str, limit: int = Query(50, ge=1, le=500)
) -> CreditsRead:
    """(관리자) 사용자의 크레딧 잔액과 내역."""
    user = await _user_by_email(session, email)
    return await _credits_of(session, settings, user.id, limit)


@router.post("/admin/credits", response_model=CreditsRead, status_code=status.HTTP_201_CREATED)
async def grant_credits(
    body: CreditGrant, session: SessionDep, settings: SettingsDep, admin: AdminUserDep
) -> CreditsRead:
    """(관리자) 크레딧 충전. 음수면 조정(회수). 결제 연동 전 초기 고객 운영용."""
    user = await _user_by_email(session, body.email)
    session.add(
        CreditTransaction(
            user_id=user.id,
            kind="grant" if body.amount > 0 else "adjust",
            amount_milli=credits_to_milli(body.amount),
            note=body.note,
            created_by=admin.id,
        )
    )
    await session.commit()
    return await _credits_of(session, settings, user.id, 50)
