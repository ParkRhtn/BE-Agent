from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select, update

from be_agent.api.deps import CurrentUserDep, SessionDep, SettingsDep
from be_agent.core.config import Settings
from be_agent.core.security import create_access_token, hash_password, verify_password
from be_agent.db.models import Agent, Thread, User
from be_agent.schemas.auth import Credentials, LoginRequest, Token, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token(settings: Settings, user: User) -> Token:
    token = create_access_token(user.id, secret=settings.jwt_secret, expire_minutes=settings.jwt_expire_minutes)
    return Token(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
async def signup(body: Credentials, session: SessionDep, settings: SettingsDep) -> Token:
    if not settings.allow_signup:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "회원가입이 비활성화되어 있습니다.")
    email = body.email.lower()
    if await session.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입된 이메일입니다.")

    is_first_user = not await session.scalar(select(func.count()).select_from(User))
    user = User(email=email, password_hash=hash_password(body.password))
    session.add(user)
    await session.flush()
    if is_first_user:
        # 인증 도입 전에 만든 데이터는 첫 사용자에게 귀속시킨다.
        for model in (Thread, Agent):
            await session.execute(update(model).where(model.user_id.is_(None)).values(user_id=user.id))
    await session.commit()
    return _issue_token(settings, user)


@router.post("/login", response_model=Token)
async def login(body: LoginRequest, session: SessionDep, settings: SettingsDep) -> Token:
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "이메일 또는 비밀번호가 올바르지 않습니다.")
    return _issue_token(settings, user)


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUserDep) -> User:
    return user
