from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import parse_sse


def node(id: str, type: str, **data: Any) -> dict[str, Any]:
    return {"id": id, "type": type, "position": {"x": 0, "y": 0}, "data": data}


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}-{handle}", "source": source, "target": target, "sourceHandle": handle}


def _create(client: TestClient, nodes: list, edges: list) -> str:
    response = client.post("/api/v1/workflows", json={"name": "wf", "graph": {"nodes": nodes, "edges": edges}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _run(client: TestClient, workflow_id: str, text: str) -> list[dict[str, Any]]:
    response = client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": text})
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def _status(events: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for e in events:
        if "node_id" in e and e["type"] != "node_delta":
            result[e["node_id"]] = e["type"]
    return result


# start → llm → condition ─true→ end_yes
#                          └false→ tool → end_no
BRANCHING = (
    [
        node("start", "start"),
        node("llm_1", "llm", prompt="요약: {{input}}", model="fake:echo"),
        node("cond_1", "condition", left="{{llm_1}}", operator="contains", right="hello"),
        node("end_yes", "end", output="YES {{llm_1}}"),
        node("tool_1", "tool", tool="get_current_time", args='{"timezone": "UTC"}'),
        node("end_no", "end"),
    ],
    [
        edge("start", "llm_1"),
        edge("llm_1", "cond_1"),
        edge("cond_1", "end_yes", "true"),
        edge("cond_1", "tool_1", "false"),
        edge("tool_1", "end_no"),
    ],
)


def test_default_graph_passes_input_through(client: TestClient) -> None:
    workflow = client.post("/api/v1/workflows", json={"name": "기본"}).json()
    assert [n["type"] for n in workflow["graph"]["nodes"]] == ["start", "end"]
    events = _run(client, workflow["id"], "안녕")
    assert events[-1] == {"type": "run_finish", "output": "안녕"}


def test_condition_true_branch(client: TestClient) -> None:
    events = _run(client, _create(client, *BRANCHING), "hello world")
    assert _status(events) == {
        "start": "node_finish",
        "llm_1": "node_finish",
        "cond_1": "node_finish",
        "end_yes": "node_finish",
        "tool_1": "node_skip",
        "end_no": "node_skip",
    }
    assert any(e["type"] == "node_delta" and e["node_id"] == "llm_1" for e in events)  # LLM 은 스트리밍
    assert events[-1]["output"] == "YES (fake 모델) 입력하신 내용: 요약: hello world"


def test_condition_false_branch_runs_tool(client: TestClient) -> None:
    events = _run(client, _create(client, *BRANCHING), "bye")
    status = _status(events)
    assert status["end_yes"] == "node_skip"
    assert status["tool_1"] == status["end_no"] == "node_finish"
    assert events[-1]["output"].endswith("+00:00")  # UTC 시각


def test_end_after_condition_passes_value_not_boolean(client: TestClient) -> None:
    nodes = [node("start", "start"), node("cond", "condition", operator="not_empty"), node("end", "end")]
    edges = [edge("start", "cond"), edge("cond", "end", "true")]
    events = _run(client, _create(client, nodes, edges), "값")
    assert events[-1]["output"] == "값"


def test_agent_node(client: TestClient) -> None:
    agent = client.post(
        "/api/v1/agents", json={"name": "a", "system_prompt": "p", "tools": ["get_current_time"]}
    ).json()
    nodes = [node("start", "start"), node("agent_1", "agent", agent_id=agent["id"]), node("end", "end")]
    events = _run(client, _create(client, nodes, [edge("start", "agent_1"), edge("agent_1", "end")]), "지금 몇 시야?")
    assert events[-1]["output"].startswith("도구 실행 결과입니다:")
    # 워크플로우 실행은 대화 스레드를 남기지 않는다
    assert client.get("/api/v1/threads").json() == []


def test_validation_errors(client: TestClient) -> None:
    nodes = [
        node("start", "start"),
        node("llm_1", "llm", prompt=""),
        node("tool_1", "tool", tool="nope", args="[1]"),
        node("agent_1", "agent", agent_id="other"),
    ]
    edges = [edge("start", "llm_1"), edge("llm_1", "tool_1"), edge("tool_1", "llm_1")]
    response = client.post(f"/api/v1/workflows/{_create(client, nodes, edges)}/run", json={"input": "x"})
    assert response.status_code == 400
    detail = " ".join(response.json()["detail"])
    for expected in (
        "종료 노드",
        "순환",
        "[llm_1] 프롬프트",
        "[tool_1] 도구를 선택",
        "JSON 객체",
        "[agent_1] 에이전트",
    ):
        assert expected in detail, expected


def test_node_error_stops_run(client: TestClient) -> None:
    nodes = [node("start", "start"), node("llm_1", "llm", prompt="x", model="anthropic:claude-sonnet-5")]
    nodes.append(node("end", "end"))
    events = _run(client, _create(client, nodes, [edge("start", "llm_1"), edge("llm_1", "end")]), "x")
    assert events[-1]["type"] == "node_error" and events[-1]["node_id"] == "llm_1"


def test_crud_and_isolation(client: TestClient) -> None:
    workflow_id = _create(client, *BRANCHING)
    renamed = client.patch(f"/api/v1/workflows/{workflow_id}", json={"name": "새 이름"}).json()
    assert renamed["name"] == "새 이름" and len(renamed["graph"]["nodes"]) == 6

    from tests.conftest import login_as

    login_as(client, "other@example.com")
    assert client.get(f"/api/v1/workflows/{workflow_id}").status_code == 404
    assert client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "x"}).status_code == 404
