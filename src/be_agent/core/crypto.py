"""API 키 같은 비밀값을 DB 에 넣기 전 암호화한다."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, secret: str) -> None:
        # 임의 길이 비밀값에서 Fernet 키(32바이트)를 만든다
        key = base64.urlsafe_b64encode(hashlib.sha256(f"provider-keys:{secret}".encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                "저장된 키를 복호화할 수 없습니다. ENCRYPTION_KEY(또는 JWT_SECRET)가 바뀌었는지 확인하세요."
            ) from exc


def key_hint(api_key: str) -> str:
    """화면에 보여줄 키 끝자리."""
    return f"…{api_key[-4:]}" if len(api_key) > 8 else "…"
