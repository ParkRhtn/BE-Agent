"""비밀번호 해시와 JWT 발급/검증."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

_password_hash = PasswordHash.recommended()
_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _password_hash.verify(password, hashed)


def create_access_token(user_id: str, *, secret: str, expire_minutes: int) -> str:
    now = datetime.now(UTC)
    # iat 는 소수점까지 기록해서, 비밀번호 변경 직전(같은 초)에 발급된 토큰도 구분한다.
    payload = {"sub": user_id, "iat": now.timestamp(), "exp": now + timedelta(minutes=expire_minutes)}
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


@dataclass(frozen=True)
class TokenClaims:
    user_id: str
    issued_at: datetime


def decode_access_token(token: str, *, secret: str) -> TokenClaims | None:
    """유효하면 클레임, 만료/위조면 None."""
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALGORITHM], options={"require": ["sub", "exp", "iat"]})
    except jwt.InvalidTokenError:
        return None
    return TokenClaims(user_id=payload["sub"], issued_at=datetime.fromtimestamp(payload["iat"], UTC))


def as_utc(value: datetime) -> datetime:
    """SQLite 는 timezone 을 버리고 돌려주므로 UTC 로 간주한다."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def new_reset_token() -> tuple[str, str]:
    """(메일로 보낼 원문, DB 에 저장할 해시)"""
    token = secrets.token_urlsafe(32)
    return token, hash_reset_token(token)


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


API_KEY_PREFIX = "sk-be-"


def new_api_key() -> tuple[str, str, str]:
    """(한 번만 보여 줄 원문, DB 에 저장할 해시, 목록에 보여 줄 앞부분)"""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_api_key(key), key[: len(API_KEY_PREFIX) + 4]


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
