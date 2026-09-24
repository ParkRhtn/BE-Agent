from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from be_agent.agent.service import AgentService
from be_agent.core.config import Settings
from be_agent.core.model_registry import ModelRegistry, load_registry
from be_agent.core.observability import Tracing
from be_agent.core.security import as_utc, decode_access_token
from be_agent.db.models import User
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


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    claims = decode_access_token(credentials.credentials, secret=settings.jwt_secret) if credentials else None
    user = await session.get(User, claims.user_id) if claims else None
    # 비밀번호 변경 이전에 발급된 토큰은 거부
    if user and claims and user.password_changed_at and claims.issued_at < as_utc(user.password_changed_at):
        user = None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다.", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_model_registry(session: SessionDep, settings: SettingsDep, user: CurrentUserDep) -> ModelRegistry:
    return await load_registry(session, user, settings)


ModelRegistryDep = Annotated[ModelRegistry, Depends(get_model_registry)]


def ensure_model(registry: ModelRegistry, model: str | None) -> None:
    """요청에 모델이 지정됐다면 이 사용자가 쓸 수 있는 모델인지 확인한다."""
    if model is not None and model not in registry.configs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"사용할 수 없는 모델입니다: {model}")
