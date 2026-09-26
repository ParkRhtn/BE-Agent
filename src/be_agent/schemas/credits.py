from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CreditTransactionRead(BaseModel):
    id: str
    kind: Literal["grant", "usage", "adjust"]
    amount: float  # 크레딧. 충전 +, 사용 -
    run_id: str | None
    note: str | None
    created_at: datetime


class CreditsRead(BaseModel):
    balance: float
    enforced: bool  # false 면 잔액이 없어도 서버 키 모델을 막지 않는다
    credits_per_usd: float  # 원가 1 USD 당 차감 크레딧
    transactions: list[CreditTransactionRead]  # 최근 순


class CreditGrant(BaseModel):
    email: str
    # 음수면 조정(회수). 0.001 크레딧 단위까지
    amount: float = Field(ge=-1_000_000_000, le=1_000_000_000)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("amount")
    @classmethod
    def _not_zero(cls, value: float) -> float:
        if round(value, 3) == 0:
            raise ValueError("0 이 아닌 값을 입력하세요.")
        return value
