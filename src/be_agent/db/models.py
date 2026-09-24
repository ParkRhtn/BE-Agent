import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Dialect, ForeignKey, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite 는 시간대를 버리고 돌려준다.

    UTC 로 바꿔 넣고, 읽을 때 UTC 를 붙여 API 가 항상 +00:00 이 붙은 시각을 내보내게 한다.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        # 항상 UTC 로 바꿔 넣는다. SQLite 는 시각을 글자로 비교하므로 한국 시간(+09:00)이 섞이면 순서가 틀어진다.
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # 이 시각 이전에 발급된 JWT 는 거부한다 (비밀번호 변경 시 다른 세션 로그아웃).
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    default_model: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class PasswordResetToken(Base):
    """비밀번호 재설정 토큰. 원문은 메일로만 보내고 DB 에는 해시만 저장한다."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Thread(Base):
    """대화 스레드 메타데이터. 메시지 본문은 LangGraph 체크포인트에 저장된다."""

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(100))
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agents.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class Agent(Base):
    """사용자가 정의한 에이전트: 시스템 프롬프트 + 모델 + 사용할 도구 묶음."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    system_prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class Workflow(Base):
    """노드·엣지 그래프(React Flow 형식)로 정의한 워크플로우."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    graph: Mapped[dict] = mapped_column(JSON)
    # 예약 실행 {enabled, time: "HH:MM", weekdays: [0=월 … 6=일], input}. 한국 시간 기준
    schedule: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    # 마지막으로 예약 실행을 맡은 예정 시각. 같은 시각에 두 번 돌지 않게 한다
    schedule_last_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # 켜 두면 삭제 API 가 거부한다. 기존 DB 에 컬럼을 붙일 수 있게 nullable (None = 꺼짐)
    delete_protected: Mapped[bool | None] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class ModelProvider(Base):
    """사용자가 등록한 모델 제공사. API 키는 암호화해 저장하고, 연결 확인 때 받은 모델 목록을 함께 둔다."""

    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # anthropic | openai | openai_compatible
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str | None] = mapped_column(String(500))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    api_key_hint: Mapped[str | None] = mapped_column(String(20))
    available_models: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{id, label}]
    enabled_models: Mapped[list[str]] = mapped_column(JSON, default=list)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class Run(Base):
    """실행 한 번 (채팅 답변 한 번 또는 워크플로우 실행 한 번). 평가와 Langfuse 기록을 잇는다."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # chat | workflow
    trigger: Mapped[str | None] = mapped_column(String(20))  # manual | schedule (워크플로우)
    thread_id: Mapped[str | None] = mapped_column(String(36), index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trace_id: Mapped[str] = mapped_column(String(32))
    feedback: Mapped[int | None] = mapped_column()  # 1 = 좋아요, -1 = 별로
    feedback_comment: Mapped[str | None] = mapped_column(Text)
    # 워크플로우 실행 기록 (채팅은 대화 이력이 따로 있어 비워 둔다)
    status: Mapped[str | None] = mapped_column(String(20))  # running | done | error | cancelled
    input: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[list[dict] | None] = mapped_column(JSON)  # [{node_id, status, output?, error?}] 실행 순서
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class UsageRecord(Base):
    """모델 호출 한 번의 토큰·비용. 사용량 화면이 이 표를 모은다."""

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    source: Mapped[str] = mapped_column(String(200))  # "대화: 에이전트 이름", "워크플로우: 이름" …
    model: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float | None] = mapped_column()  # USD. 가격표에 없는 모델이면 None
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)


class TelegramLink(Base):
    """사용자가 연결한 텔레그램 봇과, 메시지를 받을 자기 채팅."""

    __tablename__ = "telegram_links"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    bot_token_encrypted: Mapped[str] = mapped_column(Text)
    token_hint: Mapped[str] = mapped_column(String(20))
    bot_username: Mapped[str] = mapped_column(String(100))
    chat_id: Mapped[str] = mapped_column(String(50))
    chat_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)
