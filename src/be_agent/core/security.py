"""비밀번호 해시와 JWT 발급/검증."""

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
    payload = {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=expire_minutes)}
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, *, secret: str) -> str | None:
    """유효하면 user_id, 만료/위조면 None."""
    try:
        payload = jwt.decode(token, secret, algorithms=[_ALGORITHM], options={"require": ["sub", "exp"]})
    except jwt.InvalidTokenError:
        return None
    return payload["sub"]
