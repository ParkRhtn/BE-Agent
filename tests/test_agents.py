from fastapi.testclient import TestClient

from tests.conftest import parse_sse


def _create_agent(client: TestClient, **overrides: object) -> dict:
    body = {"name": "번역가", "system_prompt": "Translate to English.", "tools": [], **overrides}
    response = client.post("/api/v1/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_list_tools(client: TestClient) -> None:
    tools = client.get("/api/v1/tools").json()
    assert "get_current_time" in [t["name"] for t in tools]


def test_agent_crud(client: TestClient) -> None:
    agent = _create_agent(client)
    assert agent["tools"] == []

    updated = client.patch(f"/api/v1/agents/{agent['id']}", json={"tools": ["get_current_time"]}).json()
    assert updated["tools"] == ["get_current_time"]
    assert [a["id"] for a in client.get("/api/v1/agents").json()] == [agent["id"]]

    assert client.delete(f"/api/v1/agents/{agent['id']}").status_code == 204
    assert client.get(f"/api/v1/agents/{agent['id']}").status_code == 404


def test_rejects_unknown_tool_and_model(client: TestClient) -> None:
    assert client.post("/api/v1/agents", json={"name": "a", "system_prompt": "p", "tools": ["nope"]}).status_code == 400
    assert client.post("/api/v1/agents", json={"name": "a", "system_prompt": "p", "model": "evil:x"}).status_code == 400


def test_agent_without_tools_does_not_call_tools(client: TestClient) -> None:
    agent = _create_agent(client, tools=[])
    thread = client.post("/api/v1/threads", json={"agent_id": agent["id"]}).json()
    assert thread["agent_id"] == agent["id"]

    events = parse_sse(client.post(f"/api/v1/threads/{thread['id']}/chat", json={"message": "지금 몇 시야?"}).text)
    types = [e["type"] for e in events if isinstance(e, dict)]
    # fake 모델은 bind_tools 를 무시하고 도구를 호출하지만, 에이전트에 도구가 없으니 실행되지 않아야 한다.
    assert "tool-output-available" not in types
    assert not any(t == "error" for t in types)


def test_delete_agent_detaches_threads(client: TestClient) -> None:
    agent = _create_agent(client)
    thread = client.post("/api/v1/threads", json={"agent_id": agent["id"]}).json()
    client.delete(f"/api/v1/agents/{agent['id']}")
    assert client.get(f"/api/v1/threads/{thread['id']}").json()["agent_id"] is None


def test_create_thread_with_unknown_agent(client: TestClient) -> None:
    assert client.post("/api/v1/threads", json={"agent_id": "missing"}).status_code == 400
