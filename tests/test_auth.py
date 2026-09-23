from fastapi.testclient import TestClient

from be_agent.core.security import create_access_token
from tests.conftest import login_as


def test_requires_token(anon_client: TestClient) -> None:
    for path in ("/api/v1/threads", "/api/v1/agents", "/api/v1/tools", "/api/v1/models", "/api/v1/auth/me"):
        assert anon_client.get(path).status_code == 401, path


def test_signup_login_me(anon_client: TestClient) -> None:
    creds = {"email": "Me@Example.com", "password": "password123"}
    assert anon_client.post("/api/v1/auth/signup", json=creds).status_code == 201
    assert anon_client.post("/api/v1/auth/signup", json=creds).status_code == 409

    assert anon_client.post("/api/v1/auth/login", json={**creds, "password": "wrong-pass"}).status_code == 401
    token = anon_client.post("/api/v1/auth/login", json={**creds, "email": "me@example.com"}).json()["access_token"]
    me = anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == "me@example.com"


def test_rejects_short_password(anon_client: TestClient) -> None:
    response = anon_client.post("/api/v1/auth/signup", json={"email": "a@b.c", "password": "short"})
    assert response.status_code == 422


def test_rejects_expired_and_forged_tokens(anon_client: TestClient) -> None:
    login_as(anon_client, "a@example.com")
    user_id = anon_client.get("/api/v1/auth/me").json()["id"]

    expired = create_access_token(user_id, secret="dev-only-insecure-jwt-secret-change-me", expire_minutes=-1)
    forged = create_access_token(user_id, secret="another-secret-that-is-long-enough-xx", expire_minutes=10)
    for token in (expired, forged, "garbage"):
        assert anon_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_users_cannot_see_each_others_data(anon_client: TestClient) -> None:
    login_as(anon_client, "alice@example.com")
    agent = anon_client.post("/api/v1/agents", json={"name": "a", "system_prompt": "p"}).json()
    thread = anon_client.post("/api/v1/threads", json={"agent_id": agent["id"]}).json()

    login_as(anon_client, "bob@example.com")
    assert anon_client.get("/api/v1/threads").json() == []
    assert anon_client.get("/api/v1/agents").json() == []
    assert anon_client.get(f"/api/v1/threads/{thread['id']}").status_code == 404
    assert anon_client.get(f"/api/v1/agents/{agent['id']}").status_code == 404
    assert anon_client.post(f"/api/v1/threads/{thread['id']}/chat", json={"message": "hi"}).status_code == 404
    # 남의 에이전트로 스레드를 만들 수 없다
    assert anon_client.post("/api/v1/threads", json={"agent_id": agent["id"]}).status_code == 400


def test_signup_can_be_disabled(anon_client: TestClient) -> None:
    anon_client.app.state.settings.allow_signup = False  # type: ignore[attr-defined]
    response = anon_client.post("/api/v1/auth/signup", json={"email": "x@example.com", "password": "password123"})
    assert response.status_code == 403
