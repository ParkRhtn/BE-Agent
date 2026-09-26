from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from be_agent.agent.service import AgentService
from be_agent.core.config import Settings
from be_agent.core.model_registry import ModelRegistry, load_registry
from be_agent.core.observability import Tracing
from be_agent.core.security import API_KEY_PREFIX, as_utc, decode_access_token, hash_api_key
from be_agent.db.models import ApiKey, User
from be_agent.workflow.runner import WorkflowRunner

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def get_tracing(request: Request) -> Tracing:
    return request.app.state.tracing


def get_sessionmaker(request: Request) -> async_sessionmaker:
    """요청이 끝난 뒤에도 쓸 DB 세션용 (스트리밍 작업 안에서 결과 저장 등)."""
    return request.app.state.sessionmaker


def get_workflow_runner(request: Request) -> WorkflowRunner:
    return request.app.state.workflow_runner


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
TracingDep = Annotated[Tracing, Depends(get_tracing)]
SessionMakerDep = Annotated[async_sessionmaker, Depends(get_sessionmaker)]
WorkflowRunnerDep = Annotated[WorkflowRunner, Depends(get_workflow_runner)]


async def _user_from_jwt(session: AsyncSession, settings: Settings, token: str) -> User | None:
    claims = decode_access_token(token, secret=settings.jwt_secret)
    user = await session.get(User, claims.user_id) if claims else None
    # 비밀번호 변경 이전에 발급된 토큰은 거부
    if user and claims and user.password_changed_at and claims.issued_at < as_utc(user.password_changed_at):
        return None
    return user


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """화면(로그인 토큰) 전용. API 키로는 계정·키 관리 같은 기능을 쓸 수 없게 여기서는 받지 않는다."""
    user = await _user_from_jwt(session, settings, credentials.credentials) if credentials else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다.", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_admin_user(user: CurrentUserDep, settings: SettingsDep) -> User:
    if user.email.lower() not in {email.lower() for email in settings.admin_emails}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "관리자만 쓸 수 있습니다.")
    return user


AdminUserDep = Annotated[User, Depends(get_admin_user)]


@dataclass(frozen=True)
class ExtAuth:
    user: User
    key: ApiKey | None  # 로그인 토큰(화면의 호출 테스트)이면 None

    def can_run(self, workflow_id: str) -> bool:
        return self.key is None or self.key.workflow_ids is None or workflow_id in self.key.workflow_ids


async def get_ext_auth(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ExtAuth:
    """외부 API (/api/v1/ext/*) 인증. API 키(sk-be-…) 또는 로그인 토큰(화면의 호출 테스트용)."""
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "API 키가 올바르지 않습니다. Authorization: Bearer sk-be-… 헤더를 확인하세요.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    token = credentials.credentials
    if not token.startswith(API_KEY_PREFIX):
        user = await _user_from_jwt(session, settings, token)
        if user is None:
            raise unauthorized
        return ExtAuth(user=user, key=None)

    key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
    user = await session.get(User, key.user_id) if key else None
    if key is None or user is None:
        raise unauthorized
    if not request.app.state.ext_rate_limiter.allow(key.id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"요청이 너무 많습니다. API 키 하나당 1분에 {settings.ext_rate_limit_per_minute}번까지입니다.",
            headers={"Retry-After": "60"},
        )
    key.last_used_at = datetime.now(UTC)
    await session.commit()
    return ExtAuth(user=user, key=key)


ExtAuthDep = Annotated[ExtAuth, Depends(get_ext_auth)]


async def get_model_registry(session: SessionDep, settings: SettingsDep, user: CurrentUserDep) -> ModelRegistry:
    return await load_registry(session, user, settings)


ModelRegistryDep = Annotated[ModelRegistry, Depends(get_model_registry)]


def ensure_model(registry: ModelRegistry, model: str | None) -> None:
    """요청에 모델이 지정됐다면 이 사용자가 쓸 수 있는 모델인지 확인한다."""
    if model is not None and model not in registry.configs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"사용할 수 없는 모델입니다: {model}")
