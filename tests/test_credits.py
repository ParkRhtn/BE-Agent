from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be_agent.core.config import Settings
from be_agent.core.fake_model import FakeToolChatModel
from be_agent.core.llm import ModelConfig
from be_agent.main import create_app
from tests.conftest import login_as, parse_sse
from tests.test_workflows import edge, node

PLATFORM = "anthropic:claude-haiku-4-5-20251001"  # .env 서버 키 모델 → 크레딧 차감
ADMIN = "admin@example.com"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
        checkpoint_sqlite_path=str(tmp_path / "checkpoints.db"),
        mcp_config_path=None,
        scheduler_enabled=False,
        anthropic_api_key="sk-ant-server-key",
        allowed_models=["fake:echo", PLATFORM, "anthropic:claude-unknown-9"],
        default_model=PLATFORM,
        credits_enforced=True,
        # fake 모델은 토큰이 적어 원가가 아주 작다. 차감이 눈에 보이도록 크게 잡는다.
        credits_per_usd=1_000_000,
        admin_emails=[ADMIN],
    )


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        # 실제 Anthropic 대신 fake 모델을 쓰되, 모델 이름과 과금 주체는 실제 경로와 똑같이 넘긴다
        def fake_factory(config: ModelConfig) -> FakeToolChatModel:
            return FakeToolChatModel(model_name=config.model, metadata={"billing": config.billing})

        c.app.state.agent_service._model_factory = fake_factory  # type: ignore[attr-defined]
        yield c


def _chat(client: TestClient, text: str, model: str | None = None) -> Any:
    thread_id = client.post("/api/v1/threads", json={}).json()["id"]
    body = {"message": text, **({"model": model} if model else {})}
    return client.post(f"/api/v1/threads/{thread_id}/chat", json=body)


def _switch(client: TestClient, email: str) -> None:
    """이 계정으로 로그인한다 (처음이면 가입)."""
    credentials = {"email": email, "password": "password123"}
    response = client.post("/api/v1/auth/signup", json=credentials)
    if response.status_code == 409:
        response = client.post("/api/v1/auth/login", json=credentials)
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"


def _grant(client: TestClient, email: str, amount: float, note: str = "테스트 충전") -> Any:
    """관리자로 충전하고 원래 계정으로 돌아온다."""
    token = client.headers["Authorization"]
    _switch(client, ADMIN)
    response = client.post("/api/v1/admin/credits", json={"email": email, "amount": amount, "note": note})
    client.headers["Authorization"] = token
    return response


def test_platform_model_is_blocked_without_credits_but_free_model_works(app_client: TestClient) -> None:
    login_as(app_client, "user@example.com")

    options = {o["id"]: o for o in app_client.get("/api/v1/models").json()["options"]}
    assert options[PLATFORM]["billing"] == "platform"
    assert "크레딧이 부족합니다" in options[PLATFORM]["blocked_reason"]
    assert options["fake:echo"]["billing"] == "free" and options["fake:echo"]["blocked_reason"] is None
    # 가격을 모르는 서버 키 모델은 원가를 차감할 수 없으므로 잔액과 무관하게 막는다
    assert "가격표에 없는 모델" in options["anthropic:claude-unknown-9"]["blocked_reason"]

    response = _chat(app_client, "hi")  # 기본 모델 = 서버 키 모델
    assert response.status_code == 402
    assert "크레딧이 부족합니다" in response.json()["detail"]

    assert parse_sse(_chat(app_client, "hi", model="fake:echo").text)[-1] == "[DONE]"
    assert app_client.get("/api/v1/credits").json()["balance"] == 0


def test_platform_usage_is_deducted_by_actual_cost(app_client: TestClient) -> None:
    login_as(app_client, "user@example.com")
    assert _grant(app_client, "user@example.com", 100).status_code == 201

    events = parse_sse(_chat(app_client, "지금 몇 시야").text)  # 도구 호출 → 모델 2번
    assert events[-1] == "[DONE]"
    run_id = events[0]["messageMetadata"]["runId"]

    usage = app_client.get("/api/v1/usage").json()
    assert usage["calls"] == 2 and usage["unpriced_calls"] == 0 and usage["total_cost"] > 0
    assert {r["name"]: r["calls"] for r in usage["by_billing"]} == {"서버 키 (크레딧 차감)": 2}

    credits = app_client.get("/api/v1/credits").json()
    charged = usage["total_cost"] * 1_000_000  # credits_per_usd
    assert credits["balance"] == pytest.approx(100 - charged, abs=0.001)
    usage_tx = credits["transactions"][0]
    assert (usage_tx["kind"], usage_tx["run_id"]) == ("usage", run_id)  # 실행 한 번 = 차감 기록 한 번
    assert usage_tx["amount"] == pytest.approx(-charged, abs=0.001)


def test_free_and_byok_usage_is_not_deducted(app_client: TestClient) -> None:
    login_as(app_client, "user@example.com")
    _grant(app_client, "user@example.com", 10)
    _chat(app_client, "hi", model="fake:echo")
    credits = app_client.get("/api/v1/credits").json()
    assert credits["balance"] == 10 and [t["kind"] for t in credits["transactions"]] == ["grant"]
    usage = app_client.get("/api/v1/usage").json()
    assert {r["name"] for r in usage["by_billing"]} == {"개발용 (무료)"}


def test_run_that_overshoots_goes_negative_and_next_run_is_blocked(app_client: TestClient) -> None:
    """차감은 실행이 끝난 뒤라 마지막 실행은 잔액을 넘을 수 있다. 그 다음부터 막힌다."""
    login_as(app_client, "user@example.com")
    _grant(app_client, "user@example.com", 0.001)
    assert _chat(app_client, "hello there").status_code == 200
    assert app_client.get("/api/v1/credits").json()["balance"] < 0
    assert _chat(app_client, "again").status_code == 402


def test_workflow_needing_credits_is_refused_before_it_starts(app_client: TestClient) -> None:
    login_as(app_client, "user@example.com")
    graph = {
        "nodes": [node("start", "start"), node("llm_1", "llm", prompt="{{input}}", model=PLATFORM), node("end", "end")],
        "edges": [edge("start", "llm_1"), edge("llm_1", "end")],
    }
    workflow_id = app_client.post("/api/v1/workflows", json={"name": "요약", "graph": graph}).json()["id"]

    response = app_client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "hello"})
    assert response.status_code == 402
    assert response.json()["detail"][0].startswith("[llm_1] 크레딧이 부족합니다")

    _grant(app_client, "user@example.com", 100)
    events = parse_sse(app_client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "hello"}).text)
    assert events[-1]["type"] == "run_finish"
    assert app_client.get("/api/v1/credits").json()["balance"] < 100


def test_only_admins_can_grant(app_client: TestClient) -> None:
    login_as(app_client, "user@example.com")
    forbidden = app_client.post("/api/v1/admin/credits", json={"email": "user@example.com", "amount": 100})
    assert forbidden.status_code == 403

    _switch(app_client, ADMIN)
    assert (
        app_client.post("/api/v1/admin/credits", json={"email": "nobody@example.com", "amount": 1}).status_code == 404
    )
    assert app_client.post("/api/v1/admin/credits", json={"email": "user@example.com", "amount": 0}).status_code == 422

    granted = app_client.post(
        "/api/v1/admin/credits", json={"email": "USER@example.com", "amount": 50.5, "note": "초기 고객"}
    ).json()
    assert granted["balance"] == 50.5
    adjusted = app_client.post("/api/v1/admin/credits", json={"email": "user@example.com", "amount": -0.5}).json()
    assert adjusted["balance"] == 50
    assert [t["kind"] for t in adjusted["transactions"]] == ["adjust", "grant"]
    assert app_client.get("/api/v1/admin/credits", params={"email": "user@example.com"}).json()["balance"] == 50


def test_signup_credits(settings: Settings) -> None:
    settings.signup_credits = 300
    with TestClient(create_app(settings)) as client:
        login_as(client, "new@example.com")
        credits = client.get("/api/v1/credits").json()
        assert credits["balance"] == 300 and credits["transactions"][0]["note"] == "가입 체험 크레딧"
