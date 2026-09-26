import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Dialect, ForeignKey, String, Text, TypeDecorator
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


class ApiKey(Base):
    """외부 서비스가 쓰는 API 키. 원문은 발급할 때 한 번만 보여 주고, DB 에는 해시만 저장한다."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(20))  # 목록에서 어떤 키인지 알아볼 앞부분 (sk-be-Ab12…)
    # 이 키로 부를 수 있는 워크플로우. None 이면 전부 (고객에게 넘길 키는 그 고객 것만 고른다)
    workflow_ids: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
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
    graph: Mapped[dict] = mapped_column(JSON)  # 편집본 (화면에서 저장한 것)
    # 배포본. 외부 API·공개 링크는 이것만 실행해서, 화면에서 고쳐도 운영 중인 연동이 바뀌지 않는다
    published_graph: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # 예약 실행 {enabled, time: "HH:MM", weekdays: [0=월 … 6=일], input}. 한국 시간 기준
    schedule: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    # 마지막으로 예약 실행을 맡은 예정 시각. 같은 시각에 두 번 돌지 않게 한다
    schedule_last_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # 켜 두면 삭제 API 가 거부한다. 기존 DB 에 컬럼을 붙일 수 있게 nullable (None = 꺼짐)
    delete_protected: Mapped[bool | None] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class Embed(Base):
    """공개 링크 (다른 웹사이트에 iframe 으로 붙이는 실행 화면). 로그인 없이 쓰이므로 권한이 가장 좁다:
    이 워크플로우의 배포본 하나만 실행하고, 허용한 사이트에서만 뜨고, 하루 횟수가 정해져 있다."""

    __tablename__ = "embeds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), unique=True, index=True
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 공개 주소 /embed/<token>
    enabled: Mapped[bool] = mapped_column(default=True)
    # iframe 을 띄울 수 있는 사이트 (https://example.com). 비어 있으면 어디서나
    allowed_origins: Mapped[list[str]] = mapped_column(JSON, default=list)
    daily_limit: Mapped[int] = mapped_column(default=100)  # 하루(한국 날짜) 실행 횟수
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
    trigger: Mapped[str | None] = mapped_column(String(20))  # manual | schedule | api | embed (워크플로우)
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
    # 누구 키로 불렀나: platform(서버 키, 크레딧 차감) | user(사용자가 등록한 키) | free(개발용)
    # 이 기능 이전 기록은 None
    billing: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)


class CreditTransaction(Base):
    """크레딧 원장. 잔액은 항상 이 표의 합계다 (충전 +, 사용 -). 기록은 고치지 않고 조정 기록을 추가한다."""

    __tablename__ = "credit_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # grant(충전) | usage(사용) | adjust(관리자 조정)
    # 1/1000 크레딧 단위 정수. 부동소수점 오차 없이 합계를 낸다.
    amount_milli: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)  # usage 일 때 어느 실행의 비용인지
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(36))  # 충전·조정한 관리자
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
