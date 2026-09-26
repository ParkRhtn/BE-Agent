import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be_agent.api.v1 import providers as providers_api
from be_agent.core.config import Settings
from be_agent.core.fake_model import FakeToolChatModel
from be_agent.core.llm import ModelConfig
from be_agent.core.providers import ProviderCheckError, ProviderModel
from tests.conftest import login_as, parse_sse

GOOD_KEY = "sk-ant-good-key-1234"


@pytest.fixture
def fake_provider_api(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """실제 API 대신: GOOD_KEY 면 모델 두 개, 아니면 인증 오류."""
    calls: list[dict[str, Any]] = []

    async def fake_list_models(kind: str, *, api_key: str | None, base_url: str | None) -> list[ProviderModel]:
        calls.append({"kind": kind, "api_key": api_key, "base_url": base_url})
        if kind == "openai_compatible" or api_key == GOOD_KEY:
            return [ProviderModel("claude-sonnet-5", "Claude Sonnet 5"), ProviderModel("claude-haiku-4-5", "Haiku")]
        raise ProviderCheckError("API 키가 올바르지 않습니다.")

    async def fake_test_model(config: ModelConfig) -> str:
        if config.model == "claude-haiku-4-5":
            raise ProviderCheckError("요청 한도 또는 크레딧이 부족합니다.")
        return "OK"

    monkeypatch.setattr(providers_api, "list_models", fake_list_models)
    monkeypatch.setattr(providers_api, "test_model", fake_test_model)
    return calls


def _add_anthropic(client: TestClient, key: str = GOOD_KEY) -> Any:
    return client.post("/api/v1/providers", json={"kind": "anthropic", "api_key": key})


def test_invalid_key_is_not_saved(client: TestClient, fake_provider_api: list) -> None:
    response = _add_anthropic(client, "sk-wrong")
    assert response.status_code == 400
    assert response.json()["detail"] == "API 키가 올바르지 않습니다."
    assert client.get("/api/v1/providers").json() == []


def test_add_provider_enable_models_and_use_in_chat(
    client: TestClient, fake_provider_api: list, settings: Settings
) -> None:
    provider = _add_anthropic(client).json()
    assert provider["api_key_hint"] == "…1234"
    assert [m["id"] for m in provider["available_models"]] == ["claude-sonnet-5", "claude-haiku-4-5"]
    assert provider["enabled_models"] == []

    # 키는 DB 에 평문으로 남지 않는다
    db = sqlite3.connect(settings.database_url.split("///", 1)[1])
    stored = db.execute("select api_key_encrypted from model_providers").fetchone()[0]
    assert GOOD_KEY not in stored

    # 없는 모델은 켤 수 없다
    bad = client.patch(f"/api/v1/providers/{provider['id']}", json={"enabled_models": ["gpt-5"]})
    assert bad.status_code == 400
    client.patch(f"/api/v1/providers/{provider['id']}", json={"enabled_models": ["claude-sonnet-5"]})

    models = client.get("/api/v1/models").json()
    model_id = f"{provider['id']}:claude-sonnet-5"
    assert models["default"] == model_id  # 직접 등록한 모델이 .env 대체 모델보다 우선
    assert {
        "id": model_id,
        "label": "Claude Sonnet 5",
        "provider": "Anthropic",
        "billing": "user",  # 사용자 키는 크레딧을 차감하지 않는다
        "blocked_reason": None,
    } in models["options"]

    # 채팅은 복호화한 키로 모델을 만든다
    seen: list[ModelConfig] = []
    service = client.app.state.agent_service  # type: ignore[attr-defined]

    def capture(config: ModelConfig) -> FakeToolChatModel:
        seen.append(config)
        return FakeToolChatModel()

    service._model_factory = capture
    thread = client.post("/api/v1/threads", json={}).json()
    events = parse_sse(client.post(f"/api/v1/threads/{thread['id']}/chat", json={"message": "hi"}).text)
    assert events[-1] == "[DONE]"
    assert seen[0] == ModelConfig(provider="anthropic", model="claude-sonnet-5", api_key=GOOD_KEY, billing="user")
    assert client.get(f"/api/v1/threads/{thread['id']}").json()["model"] == model_id


def test_set_default_model(client: TestClient, fake_provider_api: list) -> None:
    provider = _add_anthropic(client).json()
    client.patch(f"/api/v1/providers/{provider['id']}", json={"enabled_models": ["claude-sonnet-5"]})
    assert client.put("/api/v1/models/default", json={"model": "fake:echo"}).json()["default"] == "fake:echo"
    assert client.get("/api/v1/models").json()["default"] == "fake:echo"
    assert client.put("/api/v1/models/default", json={"model": "nope:x"}).status_code == 400


def test_model_test_reports_real_errors(client: TestClient, fake_provider_api: list) -> None:
    provider_id = _add_anthropic(client).json()["id"]
    ok = client.post(f"/api/v1/providers/{provider_id}/test", json={"model": "claude-sonnet-5"}).json()
    assert ok["ok"] is True and ok["reply"] == "OK"
    fail = client.post(f"/api/v1/providers/{provider_id}/test", json={"model": "claude-haiku-4-5"}).json()
    assert fail == {"ok": False, "reply": None, "error": "요청 한도 또는 크레딧이 부족합니다.", "latency_ms": None}


def test_changing_key_reverifies(client: TestClient, fake_provider_api: list) -> None:
    provider_id = _add_anthropic(client).json()["id"]
    bad = client.patch(f"/api/v1/providers/{provider_id}", json={"api_key": "sk-wrong"})
    assert bad.status_code == 400
    # 실패하면 기존 키가 그대로 남는다
    assert client.get("/api/v1/providers").json()[0]["api_key_hint"] == "…1234"
    assert client.post(f"/api/v1/providers/{provider_id}/verify").status_code == 200
    assert fake_provider_api[-1]["api_key"] == GOOD_KEY  # 저장된 키를 복호화해서 확인


def test_openai_compatible_requires_http_url(client: TestClient, fake_provider_api: list) -> None:
    body = {"kind": "openai_compatible", "base_url": "file:///etc/passwd"}
    assert client.post("/api/v1/providers", json=body).status_code == 400
    body["base_url"] = "http://localhost:11434/v1/"
    provider = client.post("/api/v1/providers", json=body).json()
    assert provider["base_url"] == "http://localhost:11434/v1"
    assert provider["api_key_hint"] is None


def test_providers_are_per_user(client: TestClient, fake_provider_api: list) -> None:
    provider_id = _add_anthropic(client).json()["id"]
    login_as(client, "other@example.com")
    assert client.get("/api/v1/providers").json() == []
    assert client.post(f"/api/v1/providers/{provider_id}/verify").status_code == 404


def _status_error(cls: type, status: int, body: dict) -> Exception:
    import httpx

    response = httpx.Response(status, json={"error": body}, request=httpx.Request("POST", "https://example.com"))
    return cls(body["message"], response=response, body=body)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (("openai", "RateLimitError", 429, "insufficient_quota"), "API 크레딧이 없습니다"),
        (("openai", "RateLimitError", 429, "rate_limit_exceeded"), "요청이 너무 많아"),
        (
            ("anthropic", "BadRequestError", 400, "Your credit balance is too low to access the API"),
            "API 크레딧이 부족합니다",
        ),
        (("anthropic", "AuthenticationError", 401, "invalid x-api-key"), "API 키가 올바르지 않습니다"),
    ],
)
def test_friendly_error_messages(error: tuple, expected: str) -> None:
    import anthropic
    import openai

    from be_agent.core.providers import _friendly

    module, cls_name, status, detail = error
    sdk = openai if module == "openai" else anthropic
    body = {"message": detail, "type": detail, "code": detail} if module == "openai" else {"message": detail}
    assert _friendly(_status_error(getattr(sdk, cls_name), status, body)).args[0].startswith(expected)
