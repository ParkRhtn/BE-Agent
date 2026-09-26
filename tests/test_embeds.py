from typing import Any

from fastapi.testclient import TestClient

from be_agent.core.config import Settings
from be_agent.main import create_app
from tests.conftest import login_as
from tests.test_workflows import edge, node

ECHO = ([node("start", "start"), node("end", "end", output="답: {{input}}")], [edge("start", "end")])


def _workflow(client: TestClient, *, publish: bool = True) -> str:
    graph = {"nodes": ECHO[0], "edges": ECHO[1]}
    workflow_id = client.post("/api/v1/workflows", json={"name": "상품 설명 만들기", "graph": graph}).json()["id"]
    if publish:
        client.post(f"/api/v1/workflows/{workflow_id}/publish")
    return workflow_id


def _embed(client: TestClient, workflow_id: str, **body: Any) -> Any:
    return client.put(f"/api/v1/workflows/{workflow_id}/embed", json=body)


def _public_run(client: TestClient, token: str, text: str = "안녕") -> Any:
    # 공개 API 는 로그인 없이 부른다
    return client.post(f"/api/v1/public/embeds/{token}/run", json={"input": text}, headers={"Authorization": ""})


def test_create_and_update_embed(client: TestClient) -> None:
    workflow_id = _workflow(client)
    assert client.get(f"/api/v1/workflows/{workflow_id}/embed").json() is None

    created = _embed(client, workflow_id, allowed_origins=["https://Shop.example.com/", "http://localhost:3000"]).json()
    assert created["allowed_origins"] == ["http://localhost:3000", "https://shop.example.com"]  # 정리해서 저장
    assert (created["enabled"], created["daily_limit"], created["today_runs"]) == (True, 100, 0)

    updated = _embed(client, workflow_id, enabled=False, daily_limit=5).json()
    assert updated["token"] == created["token"]  # 설정을 바꿔도 주소는 그대로
    assert (updated["enabled"], updated["daily_limit"], updated["allowed_origins"]) == (False, 5, [])


def test_origin_must_be_a_site_address(client: TestClient) -> None:
    workflow_id = _workflow(client)
    for bad in ["example.com", "https://example.com/path", "ftp://example.com", "https://example.com?a=1"]:
        assert _embed(client, workflow_id, allowed_origins=[bad]).status_code == 422, bad


def test_public_run_without_login(client: TestClient) -> None:
    workflow_id = _workflow(client)
    token = _embed(client, workflow_id, allowed_origins=["https://shop.example.com"]).json()["token"]

    info = client.get(f"/api/v1/public/embeds/{token}", headers={"Authorization": ""}).json()
    assert info == {
        "kind": "workflow",
        "name": "상품 설명 만들기",
        "description": None,
        "allowed_origins": ["https://shop.example.com"],
    }
    assert _public_run(client, token).json() == {"status": "done", "output": "답: 안녕", "error": None}

    # 소유자 화면에서 공개 링크 실행으로 보이고, 오늘 횟수가 올라간다
    [run] = client.get(f"/api/v1/workflows/{workflow_id}/runs").json()
    assert run["trigger"] == "embed"
    assert client.get(f"/api/v1/workflows/{workflow_id}/embed").json()["today_runs"] == 1


def test_public_link_is_404_unless_enabled_and_published(client: TestClient) -> None:
    workflow_id = _workflow(client, publish=False)
    token = _embed(client, workflow_id).json()["token"]
    assert _public_run(client, token).status_code == 404  # 배포 전

    client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert _public_run(client, token).status_code == 200

    _embed(client, workflow_id, enabled=False)
    assert _public_run(client, token).status_code == 404  # 꺼짐
    assert client.get(f"/api/v1/public/embeds/{token}", headers={"Authorization": ""}).status_code == 404

    _embed(client, workflow_id, enabled=True)
    client.delete(f"/api/v1/workflows/{workflow_id}/publish")
    assert _public_run(client, token).status_code == 404  # 배포 내림

    client.post(f"/api/v1/workflows/{workflow_id}/publish")
    assert client.delete(f"/api/v1/workflows/{workflow_id}/embed").status_code == 204
    assert _public_run(client, token).status_code == 404  # 링크 삭제
    assert _public_run(client, "no-such-token").status_code == 404


def test_public_run_uses_published_version_only(client: TestClient) -> None:
    workflow_id = _workflow(client)
    token = _embed(client, workflow_id).json()["token"]
    edited = {"nodes": [ECHO[0][0], node("end", "end", output="수정 중: {{input}}")], "edges": ECHO[1]}
    client.patch(f"/api/v1/workflows/{workflow_id}", json={"graph": edited})
    assert _public_run(client, token).json()["output"] == "답: 안녕"


def test_daily_limit(client: TestClient) -> None:
    workflow_id = _workflow(client)
    token = _embed(client, workflow_id, daily_limit=2).json()["token"]
    assert [_public_run(client, token).status_code for _ in range(3)] == [200, 200, 429]
    assert "오늘" in _public_run(client, token).json()["detail"]
    # 화면에서 직접 실행한 것은 공개 링크 횟수에 들어가지 않는다
    client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "x"})
    assert client.get(f"/api/v1/workflows/{workflow_id}/embed").json()["today_runs"] == 2


def test_per_minute_limit(settings: Settings) -> None:
    settings.embed_rate_limit_per_minute = 2
    with TestClient(create_app(settings)) as client:
        login_as(client, "user@example.com")
        token = _embed(client, _workflow(client)).json()["token"]
        assert [_public_run(client, token).status_code for _ in range(3)] == [200, 200, 429]


def test_failures_do_not_leak_details(client: TestClient) -> None:
    nodes = [
        node("start", "start"),
        node("tool_1", "tool", tool="get_current_time", args='{"timezone": 123}'),  # 실행하면 실패
        node("end", "end"),
    ]
    graph = {"nodes": nodes, "edges": [edge("start", "tool_1"), edge("tool_1", "end")]}
    workflow_id = client.post("/api/v1/workflows", json={"name": "wf", "graph": graph}).json()["id"]
    client.post(f"/api/v1/workflows/{workflow_id}/publish")
    token = _embed(client, workflow_id).json()["token"]

    body = _public_run(client, token).json()
    assert body["status"] == "error" and "tool_1" not in body["error"]  # 방문자에게는 일반 문구만
    [run] = client.get(f"/api/v1/workflows/{workflow_id}/runs").json()
    assert run["status"] == "error"  # 원인은 소유자의 실행 기록에서 본다


def test_only_owner_manages_embed(client: TestClient) -> None:
    workflow_id = _workflow(client)
    login_as(client, "other@example.com")
    assert _embed(client, workflow_id).status_code == 404
    assert client.get(f"/api/v1/workflows/{workflow_id}/embed").status_code == 404
