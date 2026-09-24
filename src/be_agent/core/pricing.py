"""모델별 토큰 가격 (USD, 100만 토큰당). 사용량 화면의 비용은 이 표로 계산한다.

모델 이름이 표의 이름과 같거나 뒤에 날짜·latest 만 붙었으면 그 가격을 쓴다
(예: "gpt-4o-mini-2024-07-18", "claude-opus-4-1-20250805"). "gpt-5.4-mini" 처럼 다른 모델은 "gpt-5" 로 치지 않는다.
표에 없는 모델은 비용을 모른다고 표시한다. 새 모델을 쓰면 제공사 가격 페이지를 보고 여기에 추가한다.
- OpenAI: https://openai.com/api/pricing
- Anthropic: https://www.anthropic.com/pricing#api
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float  # 캐시에서 읽은 입력 토큰
    cache_write: float | None = None  # 캐시에 새로 쓴 입력 토큰 (Anthropic). None 이면 일반 입력 가격


PRICES: dict[str, Price] = {
    # OpenAI
    "gpt-4o-mini": Price(0.15, 0.60, 0.075),
    "gpt-4o": Price(2.50, 10.00, 1.25),
    "gpt-4.1-nano": Price(0.10, 0.40, 0.025),
    "gpt-4.1-mini": Price(0.40, 1.60, 0.10),
    "gpt-4.1": Price(2.00, 8.00, 0.50),
    "gpt-5-nano": Price(0.05, 0.40, 0.005),
    "gpt-5-mini": Price(0.25, 2.00, 0.025),
    "gpt-5": Price(1.25, 10.00, 0.125),
    "o4-mini": Price(1.10, 4.40, 0.275),
    "o3": Price(2.00, 8.00, 0.50),
    # Anthropic
    "claude-3-5-haiku": Price(0.80, 4.00, 0.08, 1.00),
    "claude-haiku-4-5": Price(1.00, 5.00, 0.10, 1.25),
    "claude-sonnet-4": Price(3.00, 15.00, 0.30, 3.75),
    "claude-opus-4-5": Price(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-1": Price(15.00, 75.00, 1.50, 18.75),
    "claude-opus-4": Price(15.00, 75.00, 1.50, 18.75),
    # 개발용 fake 모델은 돈이 들지 않는다
    "echo": Price(0, 0, 0),
}


_VERSION_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8}|latest)$")


def find_price(model: str) -> Price | None:
    return PRICES.get(_VERSION_SUFFIX.sub("", model))


def cost_of(
    model: str, *, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> float | None:
    """input_tokens 는 캐시 토큰을 포함한 전체 입력 (LangChain usage_metadata 기준)."""
    price = find_price(model)
    if price is None:
        return None
    plain = max(input_tokens - cache_read - cache_write, 0)
    write_price = price.input if price.cache_write is None else price.cache_write
    return (
        plain * price.input + cache_read * price.cache_read + cache_write * write_price + output_tokens * price.output
    ) / 1_000_000
