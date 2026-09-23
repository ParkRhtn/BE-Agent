from fastapi.testclient import TestClient

from tests.conftest import parse_sse


def _create_thread(client: TestClient) -> str:
    response = client.post("/api/v1/threads", json={})
    assert response.status_code == 201
    return response.json()["id"]


def test_chat_streams_text_and_persists_history(client: TestClient) -> None:
    thread_id = _create_thread(client)

    response = client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": "hello"})
    assert response.status_code == 200
    assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
    events = parse_sse(response.text)
    text = "".join(e["delta"] for e in events if isinstance(e, dict) and e["type"] == "text-delta")
    assert text == "(fake 모델) 입력하신 내용: hello"
    assert events[-1] == "[DONE]"

    history = client.get(f"/api/v1/threads/{thread_id}/messages").json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert client.get(f"/api/v1/threads/{thread_id}").json()["title"] == "hello"


def test_chat_with_tool_call(client: TestClient) -> None:
    thread_id = _create_thread(client)

    events = parse_sse(client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": "지금 몇 시야?"}).text)
    types = [e["type"] for e in events if isinstance(e, dict)]
    assert "tool-input-available" in types
    assert "tool-output-available" in types
    assert types.count("start-step") == 2  # 도구 호출 step + 최종 답변 step
    assert not any(t == "error" for t in types)

    history = client.get(f"/api/v1/threads/{thread_id}/messages").json()
    tool_parts = [p for p in history[1]["parts"] if p["type"] == "tool-get_current_time"]
    assert tool_parts[0]["state"] == "output-available"


def test_rejects_unknown_model(client: TestClient) -> None:
    thread_id = _create_thread(client)
    response = client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": "hi", "model": "evil:model"})
    assert response.status_code == 400


def test_delete_thread(client: TestClient) -> None:
    thread_id = _create_thread(client)
    client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": "hi"})
    assert client.delete(f"/api/v1/threads/{thread_id}").status_code == 204
    assert client.get(f"/api/v1/threads/{thread_id}").status_code == 404


def test_timestamps_are_utc_aware(client: TestClient) -> None:
    thread = client.post("/api/v1/threads", json={}).json()
    # SQLite 에서도 시간대가 붙어 나와야 브라우저가 현지 시각으로 올바르게 바꾼다
    assert thread["created_at"].endswith(("Z", "+00:00"))
    assert client.get(f"/api/v1/threads/{thread['id']}").json()["updated_at"].endswith(("Z", "+00:00"))
