from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from be_agent.core.pricing import PRICES, cost_of, find_price
from be_agent.core.usage import DAY_TZ
from tests.conftest import login_as, parse_sse
from tests.test_workflows import BRANCHING


def _chat(client: TestClient, thread_id: str, text: str) -> list[Any]:
    return parse_sse(client.post(f"/api/v1/threads/{thread_id}/chat", json={"message": text}).text)


def test_chat_answer_id_is_stable_and_feedback_is_kept(client: TestClient) -> None:
    thread_id = client.post("/api/v1/threads", json={}).json()["id"]
    start = _chat(client, thread_id, "hi")[0]
    run_id = start["messageMetadata"]["runId"]
    assert start["messageId"] == f"msg-{run_id}"

    # 새로고침 후 이력에서도 같은 답변 ID·실행 ID·시각 (시각은 화면에 보여 준다)
    question, answer = client.get(f"/api/v1/threads/{thread_id}/messages").json()
    assert answer["id"] == f"msg-{run_id}"
    assert answer["metadata"] == {"runId": run_id, "createdAt": start["messageMetadata"]["createdAt"]}
    assert question["metadata"]["createdAt"] == answer["metadata"]["createdAt"]
    assert datetime.fromisoformat(answer["metadata"]["createdAt"]).tzinfo is not None  # 시간대 포함

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


def test_usage_is_recorded_per_model_call(client: TestClient) -> None:
    usage = client.get("/api/v1/usage").json()
    assert (usage["calls"], usage["total_tokens"]) == (0, 0) and len(usage["daily"]) == 30

    thread_id = client.post("/api/v1/threads", json={}).json()["id"]
    _chat(client, thread_id, "안녕 반가워")  # 모델 1번
    _chat(client, thread_id, "지금 몇 시야")  # 도구를 부르고 답하느라 모델 2번
    workflow_id = client.post(
        "/api/v1/workflows", json={"name": "요약", "graph": {"nodes": BRANCHING[0], "edges": BRANCHING[1]}}
    ).json()["id"]
    client.post(f"/api/v1/workflows/{workflow_id}/run", json={"input": "hello"})  # LLM 노드 1번

    usage = client.get("/api/v1/usage", params={"days": 7}).json()
    assert usage["calls"] == 4 and usage["total_tokens"] > 0
    assert usage["total_cost"] == 0 and usage["unpriced_calls"] == 0  # fake 모델은 무료
    assert [(r["name"], r["calls"]) for r in usage["by_model"]] == [("echo", 4)]
    assert {r["name"]: r["calls"] for r in usage["by_source"]} == {"대화": 3, "워크플로우: 요약": 1}
    assert len(usage["daily"]) == 7 and usage["daily"][-1]["tokens"] == usage["total_tokens"]
    assert usage["daily"][-1]["date"] == datetime.now(DAY_TZ).date().isoformat()  # 오늘은 한국 날짜

    # 다른 사용자에게는 안 보인다
    login_as(client, "other@example.com")
    assert client.get("/api/v1/usage").json()["calls"] == 0


def test_pricing() -> None:
    assert find_price("gpt-4o-mini-2024-07-18") == PRICES["gpt-4o-mini"]  # 날짜 붙은 이름
    assert find_price("gpt-4o-2024-08-06") == PRICES["gpt-4o"]  # gpt-4o-mini 와 헷갈리지 않는다
    assert find_price("claude-opus-4-1-20250805") == PRICES["claude-opus-4-1"]
    assert find_price("gpt-5.4-mini") is None  # 이름이 비슷한 다른 모델을 싼 가격으로 치지 않는다
    assert find_price("o3-mini") is None
    assert find_price("some-local-model") is None
    assert cost_of("gpt-4o-mini", input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(0.75)
    # 캐시에서 읽은 입력은 싸게 계산한다
    assert cost_of("gpt-4o-mini", input_tokens=1_000_000, output_tokens=0, cache_read=1_000_000) == pytest.approx(0.075)
    assert cost_of("some-local-model", input_tokens=10, output_tokens=10) is None
    # Anthropic 캐시 쓰기: 5분은 입력의 1.25배, 1시간은 2배
    assert cost_of(
        "claude-haiku-4-5", input_tokens=3_000_000, output_tokens=0, cache_write=1_000_000, cache_write_1h=1_000_000
    ) == pytest.approx(1.00 + 1.25 + 2.00)
    assert find_price("claude-sonnet-5") == PRICES["claude-sonnet-5"]


def test_usage_callback_prices_anthropic_cache_writes() -> None:
    """실제 Anthropic 응답 형태(5분/1시간 캐시 쓰기 구분)를 LangChain 변환 그대로 넣어 비용을 확인한다."""
    from uuid import uuid4

    from anthropic.types import CacheCreation, Usage
    from langchain_anthropic.chat_models import _create_usage_metadata
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from be_agent.core.usage import UsageCallback, collect_usage

    usage = Usage(
        input_tokens=1000,
        output_tokens=300,
        cache_read_input_tokens=2000,
        cache_creation_input_tokens=700,
        cache_creation=CacheCreation(ephemeral_5m_input_tokens=500, ephemeral_1h_input_tokens=200),
    )
    message = AIMessage(content="x", usage_metadata=_create_usage_metadata(usage))
    callback, run_id = UsageCallback(), uuid4()
    with collect_usage() as collector:
        callback.on_chat_model_start({}, [], run_id=run_id, metadata={"ls_model_name": "claude-haiku-4-5-20251001"})
        callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)

    call = collector.calls[0]
    assert (call.cache_read, call.cache_write, call.cache_write_1h) == (2000, 500, 200)
    cost = cost_of(
        call.model,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cache_read=call.cache_read,
        cache_write=call.cache_write,
        cache_write_1h=call.cache_write_1h,
    )
    # Haiku 4.5 공식 단가: 입력 $1, 캐시 읽기 $0.10, 5분 쓰기 $1.25, 1시간 쓰기 $2, 출력 $5 (100만 토큰당)
    assert cost == pytest.approx((1000 * 1 + 2000 * 0.10 + 500 * 1.25 + 200 * 2 + 300 * 5) / 1_000_000)


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
