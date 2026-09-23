from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from be_agent.agent.service import AgentService
from be_agent.core.config import Settings
from be_agent.core.security import decode_access_token
from be_agent.db.models import User

_bearer = HTTPBearer(auto_error=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_agent_service(request: Request) -> AgentService:
    return request.app.state.agent_service


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def get_current_user(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    user_id = decode_access_token(credentials.credentials, secret=settings.jwt_secret) if credentials else None
    user = await session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다.", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
