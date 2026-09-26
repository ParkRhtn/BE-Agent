from typing import Any

from fastapi.testclient import TestClient

from be_agent.core.config import Settings
from be_agent.main import create_app
from tests.conftest import login_as, parse_sse
from tests.test_workflows import BRANCHING, edge, node


def _new_key(client: TestClient, name: str = "내 서버") -> dict[str, Any]:
    response = client.post("/api/v1/api-keys", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _workflow(client: TestClient, nodes: list | None = None, edges: list | None = None, *, publish: bool = True) -> str:
    """워크플로우를 만들고 (기본으로) 배포한다. 외부 API 는 배포본만 실행한다."""
    graph = {"nodes": nodes or BRANCHING[0], "edges": edges or BRANCHING[1]}
    workflow_id = client.post("/api/v1/workflows", json={"name": "wf", "graph": graph}).json()["id"]
    if publish:
        assert client.post(f"/api/v1/workflows/{workflow_id}/publish").status_code == 200
    return workflow_id


def _run(client: TestClient, key: str, workflow_id: str, **body: Any) -> Any:
    return client.post(
        f"/api/v1/ext/workflows/{workflow_id}/run", json=body, headers={"Authorization": f"Bearer {key}"}
    )


def test_create_key_shows_secret_once(client: TestClient) -> None:
    created = _new_key(client)
    assert created["key"].startswith("sk-be-") and created["prefix"] == created["key"][:10]

    [listed] = client.get("/api/v1/api-keys").json()
    assert listed["id"] == created["id"] and "key" not in listed  # 목록에서는 원문을 다시 볼 수 없다
    assert listed["last_used_at"] is None


def test_run_workflow_returns_json(client: TestClient) -> None:
    key = _new_key(client)["key"]
    workflow_id = _workflow(client)

    response = _run(client, key, workflow_id, input="hello")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done" and body["error"] is None
    assert body["output"].startswith("YES")  # BRANCHING: 입력에 hello 가 있으면 YES 쪽 종료 노드

    # 실행 기록에 API 로 실행됐다고 남고, 키의 마지막 사용 시각이 갱신된다
    [run] = client.get(f"/api/v1/workflows/{workflow_id}/runs").json()
    assert (run["id"], run["trigger"]) == (body["run_id"], "api")
    assert client.get("/api/v1/api-keys").json()[0]["last_used_at"] is not None


def test_run_workflow_stream(client: TestClient) -> None:
    key = _new_key(client)["key"]
    events = parse_sse(_run(client, key, _workflow(client), input="hello", stream=True).text)
    assert events[0]["type"] == "run_start" and events[-1]["type"] == "run_finish"


def test_failed_node_is_reported_as_error(client: TestClient) -> None:
    key = _new_key(client)["key"]
    nodes = [
        node("start", "start"),
        node("tool_1", "tool", tool="get_current_time", args='{"timezone": 123}'),  # 잘못된 인자
        node("end", "end"),
    ]
    workflow_id = _workflow(client, nodes, [edge("start", "tool_1"), edge("tool_1", "end")])
    body = _run(client, key, workflow_id, input="x").json()
    assert body["status"] == "error" and body["error"].startswith("[tool_1]") and body["output"] is None


def test_invalid_workflow_cannot_be_published(client: TestClient) -> None:
    nodes = [node("start", "start"), node("llm_1", "llm", prompt="")]  # 프롬프트가 비어 있으면 검증 실패
    workflow_id = _workflow(client, nodes, [edge("start", "llm_1")], publish=False)
    response = client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert response.status_code == 400 and isinstance(response.json()["detail"], list)
    assert client.get(f"/api/v1/workflows/{workflow_id}").json()["published_at"] is None


def test_only_the_published_version_runs(client: TestClient) -> None:
    """화면에서 고쳐도 배포 전까지 외부 호출 결과는 그대로다."""
    key = _new_key(client)["key"]
    nodes = [node("start", "start"), node("end", "end", output="v1: {{input}}")]
    workflow_id = _workflow(client, nodes, [edge("start", "end")], publish=False)
    assert _run(client, key, workflow_id, input="x").status_code == 409  # 배포 전

    client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert _run(client, key, workflow_id, input="x").json()["output"] == "v1: x"

    edited = {"nodes": [nodes[0], node("end", "end", output="v2: {{input}}")], "edges": [edge("start", "end")]}
    client.patch(f"/api/v1/workflows/{workflow_id}", json={"graph": edited})
    workflow = client.get(f"/api/v1/workflows/{workflow_id}").json()
    assert workflow["has_unpublished_changes"] is True
    assert _run(client, key, workflow_id, input="x").json()["output"] == "v1: x"  # 아직 배포본
    # 화면 실행은 편집본
    events = parse_sse(client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "x"}).text)
    assert events[-1]["output"] == "v2: x"

    client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert client.get(f"/api/v1/workflows/{workflow_id}").json()["has_unpublished_changes"] is False
    assert _run(client, key, workflow_id, input="x").json()["output"] == "v2: x"

    assert client.delete(f"/api/v1/workflows/{workflow_id}/publish").json()["published_at"] is None
    assert _run(client, key, workflow_id, input="x").status_code == 409  # 배포를 내리면 바로 거부


def test_key_limited_to_selected_workflows(client: TestClient) -> None:
    mine, other = _workflow(client), _workflow(client)
    limited = client.post("/api/v1/api-keys", json={"name": "고객 A", "workflow_ids": [mine]}).json()
    assert limited["workflow_ids"] == [mine]
    assert _run(client, limited["key"], mine, input="hello").status_code == 200
    assert _run(client, limited["key"], other, input="hello").status_code == 404  # 허락되지 않은 워크플로우

    # 남의 워크플로우나 없는 ID 로는 키를 만들 수 없다
    assert client.post("/api/v1/api-keys", json={"name": "x", "workflow_ids": ["nope"]}).status_code == 400
    assert client.post("/api/v1/api-keys", json={"name": "x", "workflow_ids": []}).status_code == 422


def test_bad_revoked_and_foreign_keys(client: TestClient) -> None:
    created = _new_key(client)
    workflow_id = _workflow(client)
    assert _run(client, "sk-be-wrong", workflow_id).status_code == 401
    assert (
        client.post(f"/api/v1/ext/workflows/{workflow_id}/run", json={}, headers={"Authorization": ""}).status_code
        == 401
    )

    # 다른 사용자의 키로는 내 워크플로우가 없는 것처럼 보인다
    login_as(client, "other@example.com")
    other_key = _new_key(client)["key"]
    assert _run(client, other_key, workflow_id).status_code == 404

    # 폐기하면 바로 거부
    login_as(client, "third@example.com")
    assert client.delete(f"/api/v1/api-keys/{created['id']}").status_code == 404  # 남의 키는 못 지운다
    client.headers["Authorization"] = ""
    response = client.post("/api/v1/auth/login", json={"email": "user@example.com", "password": "password123"})
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    assert client.delete(f"/api/v1/api-keys/{created['id']}").status_code == 204
    assert _run(client, created["key"], workflow_id).status_code == 401


def test_api_key_cannot_use_account_endpoints(client: TestClient) -> None:
    """키가 유출돼도 새 키 발급, 제공사 키, 계정 정보에는 접근할 수 없다."""
    key = _new_key(client)["key"]
    headers = {"Authorization": f"Bearer {key}"}
    for method, path in [
        ("GET", "/api/v1/api-keys"),
        ("POST", "/api/v1/api-keys"),
        ("GET", "/api/v1/providers"),
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/workflows"),
    ]:
        assert client.request(method, path, headers=headers, json={"name": "x"}).status_code == 401, path


def test_login_token_also_works_for_testing_in_the_ui(client: TestClient) -> None:
    workflow_id = _workflow(client)
    response = client.post(f"/api/v1/ext/workflows/{workflow_id}/run", json={"input": "hello"})
    assert response.json()["status"] == "done"


def test_rate_limit_per_key(settings: Settings) -> None:
    settings.ext_rate_limit_per_minute = 2
    with TestClient(create_app(settings)) as client:
        login_as(client, "user@example.com")
        key, other = _new_key(client)["key"], _new_key(client, "다른 키")["key"]
        workflow_id = _workflow(client)
        assert [_run(client, key, workflow_id, input="hi").status_code for _ in range(3)] == [200, 200, 429]
        assert _run(client, other, workflow_id, input="hi").status_code == 200  # 키마다 따로 센다
