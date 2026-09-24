"""요청·실행 단위로 따라다니는 값. 도구처럼 호출 인자로 받기 어려운 곳에서 쓴다."""

from contextvars import ContextVar

# 지금 실행 중인 대화·워크플로우의 사용자. 사용자별 설정이 필요한 도구(텔레그램 보내기 등)가 읽는다.
current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
