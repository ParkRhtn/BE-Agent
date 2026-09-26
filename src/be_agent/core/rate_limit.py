"""간단한 요청 횟수 제한 (최근 1분 창). 서버 프로세스 메모리에 세므로, 여러 대로 늘리면 Redis 로 옮긴다."""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int, *, window: float = 60.0) -> None:
        self.per_minute = per_minute
        self._window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        return True
