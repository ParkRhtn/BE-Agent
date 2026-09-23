from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import func, select, update

from be_agent.api.deps import CurrentUserDep, SessionDep, SettingsDep
from be_agent.core.config import Settings
from be_agent.core.mailer import send_email
from be_agent.core.security import (
    as_utc,
    create_access_token,
    hash_password,
    hash_reset_token,
    new_reset_token,
    verify_password,
)
from be_agent.db.models import Agent, PasswordResetToken, Thread, User
from be_agent.schemas.auth import (
    Credentials,
    LoginRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    Token,
    UserRead,
)

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


# 같은 계정으로 재설정 메일을 연달아 보내지 않도록 하는 최소 간격
_RESET_REQUEST_COOLDOWN = timedelta(minutes=1)


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    body: PasswordResetRequest, session: SessionDep, settings: SettingsDep, background: BackgroundTasks
) -> dict[str, str]:
    """가입 여부와 관계없이 같은 응답을 준다 (이메일 존재 여부 노출 방지)."""
    accepted = {"detail": "가입된 이메일이라면 재설정 링크를 보냈습니다."}
    user = await session.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None:
        return accepted

    now = datetime.now(UTC)
    last = await session.scalar(
        select(func.max(PasswordResetToken.created_at)).where(PasswordResetToken.user_id == user.id)
    )
    if last and now - as_utc(last) < _RESET_REQUEST_COOLDOWN:
        return accepted

    token, token_hash = new_reset_token()
    expires = timedelta(minutes=settings.password_reset_expire_minutes)
    session.add(PasswordResetToken(user_id=user.id, token_hash=token_hash, expires_at=now + expires))
    await session.commit()

    link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={token}"
    background.add_task(
        send_email,
        settings,
        to=user.email,
        subject="[Agent] 비밀번호 재설정",
        body=(
            "비밀번호 재설정을 요청하셨습니다. 아래 링크에서 새 비밀번호를 설정하세요.\n\n"
            f"{link}\n\n"
            f"링크는 {settings.password_reset_expire_minutes}분 동안 한 번만 사용할 수 있습니다.\n"
            "요청하지 않으셨다면 이 메일을 무시하세요."
        ),
    )
    return accepted


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(body: PasswordResetConfirm, session: SessionDep) -> None:
    now = datetime.now(UTC)
    record = await session.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_reset_token(body.token))
    )
    if record is None or record.used_at is not None or as_utc(record.expires_at) < now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "링크가 만료되었거나 이미 사용되었습니다. 다시 요청하세요.")

    user = await session.get(User, record.user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "링크가 만료되었거나 이미 사용되었습니다. 다시 요청하세요.")

    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = now  # 기존 로그인 세션 무효화
    # 이 토큰과 아직 안 쓴 다른 재설정 토큰도 모두 사용 처리
    await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    await session.commit()
