"""Langfuse 통계 API(v2 metrics)로 사용자별 비용·토큰 사용량을 모은다. Langfuse 가 모델 가격으로 비용을 계산한다."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from be_agent.core.config import Settings
from be_agent.schemas.runs import UsageDay, UsageRead, UsageRow

_MEASURES = [
    {"measure": "totalCost", "aggregation": "sum"},
    {"measure": "totalTokens", "aggregation": "sum"},
    {"measure": "count", "aggregation": "count"},
]


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _row(name: str, item: dict[str, Any]) -> UsageRow:
    return UsageRow(
        name=name,
        cost=_num(item.get("sum_totalCost")),
        tokens=int(_num(item.get("sum_totalTokens"))),
        calls=int(_num(item.get("count_count"))),
    )


async def load_usage(settings: Settings, *, user_id: str, days: int) -> UsageRead:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return UsageRead(enabled=False, days=days)

    now = datetime.now(UTC)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # 모델 호출(GENERATION)만 센다. 이 사용자 것만.
    filters = [
        {"column": "userId", "operator": "=", "value": user_id, "type": "string"},
        {"column": "type", "operator": "=", "value": "GENERATION", "type": "string"},
    ]

    async def query(client: httpx.AsyncClient, extra: dict[str, Any]) -> list[dict[str, Any]]:
        body = {
            "view": "observations",
            "metrics": _MEASURES,
            "dimensions": [],
            "filters": filters,
            "fromTimestamp": start.isoformat(),
            "toTimestamp": now.isoformat(),
            **extra,
        }
        response = await client.get("/api/public/v2/metrics", params={"query": json.dumps(body)})
        response.raise_for_status()
        return response.json().get("data", [])

    try:
        async with httpx.AsyncClient(
            base_url=settings.langfuse_host or "https://cloud.langfuse.com",
            auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
            timeout=20,
        ) as client:
            total, models, sources, daily = await asyncio.gather(
                query(client, {}),
                query(client, {"dimensions": [{"field": "providedModelName"}]}),
                query(client, {"dimensions": [{"field": "traceName"}]}),
                query(client, {"timeDimension": {"granularity": "day"}}),
            )
    except httpx.HTTPError as exc:
        return UsageRead(enabled=True, days=days, error=f"Langfuse 에서 사용량을 가져오지 못했습니다: {exc}")

    summary = _row("합계", total[0] if total else {})

    def rows(items: list[dict[str, Any]], key: str, fallback: str) -> list[UsageRow]:
        result = [_row(item.get(key) or fallback, item) for item in items]
        return sorted([r for r in result if r.calls], key=lambda r: (r.cost, r.tokens), reverse=True)

    return UsageRead(
        enabled=True,
        days=days,
        total_cost=summary.cost,
        total_tokens=summary.tokens,
        calls=summary.calls,
        by_model=rows(models, "providedModelName", "모델 이름 없음 (개발용 fake 모델 등)"),
        by_source=rows(sources, "traceName", "기타"),
        daily=[
            UsageDay(
                date=str(item.get("time_dimension", ""))[:10],
                cost=_num(item.get("sum_totalCost")),
                tokens=int(_num(item.get("sum_totalTokens"))),
            )
            for item in daily
        ],
    )
