"""Langfuse 를 켰을 때 남는 기록 구조. 외부로 보내지 않고 메모리로 받아 검사한다."""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langfuse import LangfuseOtelSpanAttributes as A
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from be_agent.core.config import Settings
from be_agent.main import create_app
from tests.conftest import login_as, parse_sse
from tests.test_workflows import BRANCHING, edge, node


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def traced_client(tmp_path: Path, exporter: InMemorySpanExporter) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite+aiosqlite:///{tmp_path}/app.db",
        checkpoint_sqlite_path=str(tmp_path / "checkpoints.db"),
        # Langfuse 클라이언트는 공개 키마다 하나라서 테스트마다 다른 키를 쓴다
        langfuse_public_key=f"pk-test-{uuid.uuid4().hex}",
        langfuse_secret_key="sk-test",
        langfuse_host="http://127.0.0.1:9",
    )
    with TestClient(create_app(settings, trace_exporter=exporter)) as client:
        login_as(client, "traced@example.com")
        yield client


def _spans(client: TestClient, exporter: InMemorySpanExporter) -> list[Any]:
    client.app.state.tracing.client.flush()  # type: ignore[attr-defined]
    return list(exporter.get_finished_spans())


def test_workflow_run_is_one_trace_with_node_spans(traced_client: TestClient, exporter: InMemorySpanExporter) -> None:
    workflow = traced_client.post(
        "/api/v1/workflows", json={"name": "분기", "graph": {"nodes": BRANCHING[0], "edges": BRANCHING[1]}}
    )
    workflow_id = workflow.json()["id"]
    events = parse_sse(traced_client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "hello"}).text)
    assert events[-1]["type"] == "run_finish"

    spans = _spans(traced_client, exporter)
    by_name = {s.name: s for s in spans}
    root = by_name["워크플로우: 분기"]

    # 모든 기록이 하나의 트레이스로 묶이고, 사용자·세션·태그가 붙는다
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    user_id = traced_client.get("/api/v1/auth/me").json()["id"]
    assert root.attributes[A.TRACE_USER_ID] == user_id
    assert root.attributes[A.TRACE_SESSION_ID] == f"workflow-{workflow_id}"
    assert "workflow" in root.attributes[A.TRACE_TAGS]

    # 실행된 노드마다 하위 기록 (건너뛴 노드는 없음)
    assert {"start (start)", "llm_1 (llm)", "cond_1 (condition)", "end_yes (end)"} <= set(by_name)
    assert "tool_1 (tool)" not in by_name
    for name in ("llm_1 (llm)", "cond_1 (condition)"):
        assert by_name[name].parent.span_id == root.context.span_id

    # LLM 노드 안의 모델 호출(generation)이 LLM 노드 아래에 붙는다
    generations = [s for s in spans if s.attributes.get(A.OBSERVATION_TYPE) == "generation"]
    assert generations and generations[0].parent.span_id == by_name["llm_1 (llm)"].context.span_id


def test_tool_and_agent_nodes_are_traced(traced_client: TestClient, exporter: InMemorySpanExporter) -> None:
    agent = traced_client.post(
        "/api/v1/agents", json={"name": "a", "system_prompt": "p", "tools": ["get_current_time"]}
    ).json()
    nodes = [
        node("start", "start"),
        node("tool_1", "tool", tool="get_current_time", args='{"timezone": "UTC"}'),
        node("agent_1", "agent", agent_id=agent["id"], message="지금 몇 시야?"),
        node("end", "end"),
    ]
    edges = [edge("start", "tool_1"), edge("tool_1", "agent_1"), edge("agent_1", "end")]
    workflow_id = traced_client.post(
        "/api/v1/workflows", json={"name": "wf", "graph": {"nodes": nodes, "edges": edges}}
    ).json()["id"]
    parse_sse(traced_client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "x"}).text)

    by_name = {s.name: s for s in _spans(traced_client, exporter)}
    assert by_name["tool_1 (tool)"].attributes[A.OBSERVATION_TYPE] == "tool"
    assert by_name["agent_1 (agent)"].attributes[A.OBSERVATION_TYPE] == "agent"
    # 도구 호출 자체도 도구 노드 아래에 기록된다
    tool_calls = [
        s for s in by_name.values() if s.parent and s.parent.span_id == by_name["tool_1 (tool)"].context.span_id
    ]
    assert tool_calls


def test_node_error_is_marked(traced_client: TestClient, exporter: InMemorySpanExporter) -> None:
    nodes = [node("start", "start"), node("tool_1", "tool", tool="get_current_time", args='{"timezone": 123}')]
    nodes.append(node("end", "end"))
    workflow_id = traced_client.post(
        "/api/v1/workflows",
        json={"name": "실패", "graph": {"nodes": nodes, "edges": [edge("start", "tool_1"), edge("tool_1", "end")]}},
    ).json()["id"]
    parse_sse(traced_client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "x"}).text)

    by_name = {s.name: s for s in _spans(traced_client, exporter)}
    assert by_name["tool_1 (tool)"].attributes[A.OBSERVATION_LEVEL] == "ERROR"
    assert by_name["워크플로우: 실패"].attributes[A.OBSERVATION_LEVEL] == "ERROR"


def test_chat_is_grouped_by_user_and_thread(traced_client: TestClient, exporter: InMemorySpanExporter) -> None:
    thread_id = traced_client.post("/api/v1/threads", json={}).json()["id"]
    parse_sse(traced_client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": "hi"}).text)

    spans = _spans(traced_client, exporter)
    user_id = traced_client.get("/api/v1/auth/me").json()["id"]
    assert any(s.attributes.get(A.TRACE_SESSION_ID) == thread_id for s in spans)
    assert any(s.attributes.get(A.TRACE_USER_ID) == user_id for s in spans)
    assert any(s.name == "대화" for s in spans)


def test_tracing_off_without_keys(client: TestClient) -> None:
    assert client.app.state.tracing.enabled is False  # type: ignore[attr-defined]


def test_chat_trace_id_comes_from_run_and_feedback_is_scored(
    traced_client: TestClient, exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from be_agent.core.observability import Tracing

    scores: list[dict[str, Any]] = []
    tracing = traced_client.app.state.tracing  # type: ignore[attr-defined]
    monkeypatch.setattr(tracing.client, "create_score", lambda **kw: scores.append(kw))

    thread_id = traced_client.post("/api/v1/threads", json={}).json()["id"]
    events = parse_sse(traced_client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": "hi"}).text)
    run_id = events[0]["messageMetadata"]["runId"]

    root = next(s for s in _spans(traced_client, exporter) if s.name == "대화")
    assert format(root.context.trace_id, "032x") == Tracing.trace_id_for(run_id)

    traced_client.put(f"/api/v1/runs/{run_id}/feedback", json={"value": -1, "comment": "틀림"})
    assert scores == [
        {
            "name": "user-feedback",
            "value": -1.0,
            "data_type": "NUMERIC",
            "trace_id": Tracing.trace_id_for(run_id),
            "score_id": f"{Tracing.trace_id_for(run_id)}-user-feedback",
            "comment": "틀림",
        }
    ]
