import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be_agent.api.v1 import auth
from tests.conftest import login_as

EMAIL = "reset@example.com"


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def fake_send_email(settings: Any, **kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(auth, "send_email", fake_send_email)
    return sent


def _token_from(mail: dict[str, Any]) -> str:
    match = re.search(r"/reset-password\?token=([\w-]+)", mail["body"])
    assert match
    return match.group(1)


def test_reset_flow(anon_client: TestClient, outbox: list[dict[str, Any]]) -> None:
    login_as(anon_client, EMAIL, "old-password")
    old_session = dict(anon_client.headers)

    response = anon_client.post("/api/v1/auth/password-reset/request", json={"email": EMAIL.upper()})
    assert response.status_code == 202
    assert len(outbox) == 1 and outbox[0]["to"] == EMAIL
    token = _token_from(outbox[0])

    confirm = {"token": token, "new_password": "new-password"}
    assert anon_client.post("/api/v1/auth/password-reset/confirm", json=confirm).status_code == 204
    # 한 번만 쓸 수 있다
    assert anon_client.post("/api/v1/auth/password-reset/confirm", json=confirm).status_code == 400

    login = anon_client.post
    assert login("/api/v1/auth/login", json={"email": EMAIL, "password": "old-password"}).status_code == 401
    assert login("/api/v1/auth/login", json={"email": EMAIL, "password": "new-password"}).status_code == 200
    # 재설정 전에 발급된 세션은 무효
    assert anon_client.get("/api/v1/auth/me", headers=old_session).status_code == 401


def test_unknown_email_gets_same_response(anon_client: TestClient, outbox: list[dict[str, Any]]) -> None:
    known = anon_client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert known.status_code == 202
    assert outbox == []


def test_cooldown(anon_client: TestClient, outbox: list[dict[str, Any]]) -> None:
    login_as(anon_client, EMAIL)
    for _ in range(3):
        anon_client.post("/api/v1/auth/password-reset/request", json={"email": EMAIL})
    assert len(outbox) == 1


def test_invalid_and_expired_token(anon_client: TestClient, outbox: list[dict[str, Any]]) -> None:
    confirm = anon_client.post
    body = {"token": "bogus", "new_password": "new-password"}
    assert confirm("/api/v1/auth/password-reset/confirm", json=body).status_code == 400

    anon_client.app.state.settings.password_reset_expire_minutes = -1  # type: ignore[attr-defined]
    login_as(anon_client, EMAIL)
    anon_client.post("/api/v1/auth/password-reset/request", json={"email": EMAIL})
    body["token"] = _token_from(outbox[0])
    assert confirm("/api/v1/auth/password-reset/confirm", json=body).status_code == 400


def test_short_new_password(anon_client: TestClient) -> None:
    body = {"token": "x", "new_password": "short"}
    assert anon_client.post("/api/v1/auth/password-reset/confirm", json=body).status_code == 422
