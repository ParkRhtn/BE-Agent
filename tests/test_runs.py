import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from be_agent.core import usage as usage_module
from be_agent.core.config import Settings
from be_agent.core.usage import load_usage
from tests.conftest import login_as, parse_sse
from tests.test_workflows import BRANCHING


def _chat(client: TestClient, thread_id: str, text: str) -> list[Any]:
    return parse_sse(client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": text}).text)


def test_chat_answer_id_is_stable_and_feedback_is_kept(client: TestClient) -> None:
    thread_id = client.post("/api/v1/threads", json={}).json()["id"]
    start = _chat(client, thread_id, "hi")[0]
    run_id = start["messageMetadata"]["runId"]
    assert start["messageId"] == f"msg-{run_id}"

    # 새로고침 후 이력에서도 같은 답변 ID·실행 ID
    answer = client.get(f"/api/v1/threads/{thread_id}/messages").json()[1]
    assert answer["id"] == f"msg-{run_id}"
    assert answer["metadata"] == {"runId": run_id}

    assert client.put(f"/api/v1/runs/{run_id}/feedback", json={"value": -1}).json() == {
        "run_id": run_id,
        "feedback": -1,
    }
    client.put(f"/api/v1/runs/{run_id}/feedback", json={"value": 1, "comment": "좋아요"})
    answer = client.get(f"/api/v1/threads/{thread_id}/messages").json()[1]
    assert answer["metadata"]["feedback"] == 1


def test_each_turn_gets_its_own_run(client: TestClient) -> None:
    thread_id = client.post("/api/v1/threads", json={}).json()["id"]
    first = _chat(client, thread_id, "하나")[0]["messageMetadata"]["runId"]
    second = _chat(client, thread_id, "둘")[0]["messageMetadata"]["runId"]
    history = client.get(f"/api/v1/threads/{thread_id}/messages").json()
    assert [m["metadata"]["runId"] for m in history if m["role"] == "assistant"] == [first, second]


def test_workflow_run_feedback(client: TestClient) -> None:
    body = {"name": "wf", "graph": {"nodes": BRANCHING[0], "edges": BRANCHING[1]}}
    workflow_id = client.post("/api/v1/workflows", json=body).json()["id"]
    events = parse_sse(client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "hello"}).text)
    run_id = events[0]["run_id"]
    assert client.put(f"/api/v1/runs/{run_id}/feedback", json={"value": 1}).status_code == 200


def test_feedback_validation_and_ownership(client: TestClient) -> None:
    thread_id = client.post("/api/v1/threads", json={}).json()["id"]
    run_id = _chat(client, thread_id, "hi")[0]["messageMetadata"]["runId"]
    assert client.put(f"/api/v1/runs/{run_id}/feedback", json={"value": 5}).status_code == 422
    login_as(client, "other@example.com")
    assert client.put(f"/api/v1/runs/{run_id}/feedback", json={"value": 1}).status_code == 404


def test_usage_disabled_without_langfuse(client: TestClient) -> None:
    assert client.get("/api/v1/usage").json()["enabled"] is False


async def test_usage_aggregates_langfuse_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.url.params["query"])
        seen.append(query)
        dims = [d["field"] for d in query["dimensions"]]
        if "providedModelName" in dims:
            data = [
                {
                    "providedModelName": "gpt-4o-mini",
                    "sum_totalCost": 0.002,
                    "sum_totalTokens": "900",
                    "count_count": "3",
                },
                {"providedModelName": None, "sum_totalCost": 0, "sum_totalTokens": "0", "count_count": "0"},
            ]
        elif "traceName" in dims:
            data = [
                {"traceName": "워크플로우: 요약", "sum_totalCost": 0.002, "sum_totalTokens": "900", "count_count": "3"}
            ]
        elif "timeDimension" in query:
            data = [{"time_dimension": "2026-09-23", "sum_totalCost": 0.002, "sum_totalTokens": "900"}]
        else:
            data = [{"sum_totalCost": 0.002, "sum_totalTokens": "900", "count_count": "3"}]
        return httpx.Response(200, json={"data": data})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        usage_module.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    settings = Settings(_env_file=None, langfuse_public_key="pk", langfuse_secret_key="sk")  # type: ignore[call-arg]
    usage = await load_usage(settings, user_id="u1", days=7)

    assert usage.enabled and usage.error is None
    assert (usage.total_cost, usage.total_tokens, usage.calls) == (0.002, 900, 3)
    assert [r.name for r in usage.by_model] == ["gpt-4o-mini"]  # 호출이 없는 줄은 뺀다
    assert usage.by_source[0].name == "워크플로우: 요약"
    assert usage.daily[0].date == "2026-09-23"
    # 이 사용자의 모델 호출만 센다
    assert all({"column": "userId", "operator": "=", "value": "u1", "type": "string"} in q["filters"] for q in seen)


def test_workflow_run_history(client: TestClient) -> None:
    from tests.test_workflows import edge, node

    body = {"name": "wf", "graph": {"nodes": BRANCHING[0], "edges": BRANCHING[1]}}
    workflow_id = client.post("/api/v1/workflows", json=body).json()["id"]
    first = parse_sse(client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "hello"}).text)[0]["run_id"]
    second = parse_sse(client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "bye"}).text)[0]["run_id"]

    runs = client.get(f"/api/v1/workflows/{workflow_id}/runs").json()
    assert [r["id"] for r in runs] == [second, first]  # 새것부터
    assert runs[1]["status"] == "done" and runs[1]["input"] == "hello"
    assert runs[1]["output"].startswith("YES")
    assert runs[1]["finished_at"] is not None

    detail = client.get(f"/api/v1/runs/{first}").json()
    status = {s["node_id"]: s["status"] for s in detail["steps"]}
    assert status == {
        "start": "done",
        "llm_1": "done",
        "cond_1": "done",
        "end_yes": "done",
        "tool_1": "skipped",
        "end_no": "skipped",
    }
    llm = next(s for s in detail["steps"] if s["node_id"] == "llm_1")
    assert llm["output"] == "(fake 모델) 입력하신 내용: 요약: hello"

    # 실패한 실행도 남는다
    nodes = [node("start", "start"), node("tool_1", "tool", tool="get_current_time", args='{"timezone": 123}')]
    nodes.append(node("end", "end"))
    bad = client.post(
        "/api/v1/workflows",
        json={"name": "bad", "graph": {"nodes": nodes, "edges": [edge("start", "tool_1"), edge("tool_1", "end")]}},
    ).json()["id"]
    parse_sse(client.post(f"/api/v1/workflows/{bad}/run", json={"input": "x"}).text)
    failed = client.get(f"/api/v1/workflows/{bad}/runs").json()[0]
    assert failed["status"] == "error" and failed["error"].startswith("tool_1:")

    login_as(client, "other@example.com")
    assert client.get(f"/api/v1/runs/{first}").status_code == 404
    assert client.get(f"/api/v1/workflows/{workflow_id}/runs").status_code == 404
