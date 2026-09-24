"""예약 실행 시각 계산. 시각과 요일은 한국 시간 기준이다."""

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

SCHEDULE_TZ = ZoneInfo("Asia/Seoul")


def _slot(day: date, schedule: dict[str, Any]) -> datetime | None:
    """그날 실행할 시각. 요일이 아니면 None."""
    if day.weekday() not in schedule.get("weekdays", []):
        return None
    hour, minute = map(int, schedule["time"].split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=SCHEDULE_TZ)


def last_due(schedule: dict[str, Any], now: datetime) -> datetime | None:
    """now 이전(포함) 가장 최근의 예정 시각 (최근 7일 안)."""
    today = now.astimezone(SCHEDULE_TZ).date()
    for back in range(8):
        slot = _slot(today - timedelta(days=back), schedule)
        if slot is not None and slot <= now:
            return slot
    return None


def next_due(schedule: dict[str, Any], now: datetime) -> datetime | None:
    """now 이후 가장 가까운 예정 시각. 꺼져 있거나 요일이 없으면 None."""
    if not schedule.get("enabled"):
        return None
    today = now.astimezone(SCHEDULE_TZ).date()
    for ahead in range(8):
        slot = _slot(today + timedelta(days=ahead), schedule)
        if slot is not None and slot > now:
            return slot
    return None
