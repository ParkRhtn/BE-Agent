"""LangGraph 기반 실행 엔진의 동작 (API 를 거치지 않고 엔진만)."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.tools import StructuredTool

from be_agent.schemas.workflows import WorkflowGraph
from be_agent.workflow.engine import RunEvent, WorkflowExecutor
from tests.test_workflows import edge, node


class _Trace:
    def finish(self, *, output: Any = None, error: str | None = None) -> None: ...


class _Runtime:
    """도구 호출마다 시작·끝 시각을 남기는 테스트용 런타임."""

    callbacks: list[Any] = []

    def __init__(self) -> None:
        self.calls: dict[str, tuple[float, float]] = {}

        async def slow(label: str) -> str:
            started = time.perf_counter()
            await asyncio.sleep(0.2)
            self.calls[label] = (started, time.perf_counter())
            return f"{label} 완료"

        self._tool = StructuredTool.from_function(coroutine=slow, name="slow", description="0.2초 걸리는 도구")

    @contextmanager
    def trace_node(self, node_id: str, kind: str, input: Any) -> Iterator[_Trace]:
        yield _Trace()

    def create_model(self, model_id: str | None) -> Any:
        raise AssertionError("이 테스트는 모델을 쓰지 않는다")

    def get_tool(self, name: str) -> Any:
        return self._tool

    async def run_agent(self, spec: Any, message: str) -> str:
        return message


async def _run(nodes: list, edges: list, text: str = "x") -> tuple[list[RunEvent], _Runtime]:
    runtime = _Runtime()
    executor = WorkflowExecutor(WorkflowGraph.model_validate({"nodes": nodes, "edges": edges}), runtime, {})
    return [e async for e in executor.run(text)], runtime


def _slow(id: str) -> dict[str, Any]:
    return node(id, "tool", tool="slow", args=f'{{"label": "{id}"}}')


async def test_independent_branches_run_in_parallel_and_join_once() -> None:
    #        ┌ a ┐
    # start ─┤   ├─ end
    #        └ b ┘
    nodes = [node("start", "start"), _slow("a"), _slow("b"), node("end", "end", output="{{a}} / {{b}}")]
    edges = [edge("start", "a"), edge("start", "b"), edge("a", "end"), edge("b", "end")]
    started = time.perf_counter()
    events, runtime = await _run(nodes, edges)

    assert time.perf_counter() - started < 0.35  # 순서대로면 0.4초 이상
    (a_start, a_end), (b_start, b_end) = runtime.calls["a"], runtime.calls["b"]
    assert a_start < b_end and b_start < a_end  # 실행 구간이 겹친다
    assert [e.node_id for e in events if e.type == "node_start"].count("end") == 1
    assert events[-1].output == "a 완료 / b 완료"


async def test_join_after_condition_runs_once_with_taken_branch() -> None:
    # start → cond ─참→ yes ┐
    #              └거짓→ no ┴→ end
    nodes = [
        node("start", "start"),
        node("cond", "condition", operator="contains", right="예"),
        node("yes", "tool", tool="slow", args='{"label": "yes"}'),
        node("no", "tool", tool="slow", args='{"label": "no"}'),
        node("end", "end"),
    ]
    edges = [edge("start", "cond"), edge("cond", "yes", "true"), edge("cond", "no", "false")]
    edges += [edge("yes", "end"), edge("no", "end")]
    events, _ = await _run(nodes, edges, "예")

    status = {e.node_id: e.type for e in events if e.node_id and e.type != "node_delta"}
    assert status == {
        "start": "node_finish",
        "cond": "node_finish",
        "yes": "node_finish",
        "no": "node_skip",
        "end": "node_finish",
    }
    assert events[-1] == RunEvent(type="run_finish", output="yes 완료")


async def test_long_chain_beyond_default_step_limit() -> None:
    # LangGraph 기본 단계 제한(25)보다 긴 흐름
    ids = [f"n{i}" for i in range(30)]
    nodes = [node("start", "start"), *[node(i, "condition", operator="not_empty") for i in ids], node("end", "end")]
    chain = ["start", *ids, "end"]
    edges = [edge(chain[0], chain[1])] + [edge(a, b, "true") for a, b in zip(chain[1:], chain[2:], strict=False)]
    events, _ = await _run(nodes, edges, "값")
    assert events[-1] == RunEvent(type="run_finish", output="값")


async def test_run_start_carries_run_id() -> None:
    runtime = _Runtime()
    graph = WorkflowGraph.model_validate(
        {"nodes": [node("start", "start"), node("end", "end")], "edges": [edge("start", "end")]}
    )
    events = [e async for e in WorkflowExecutor(graph, runtime, {}).run("x", run_id="r1")]
    assert events[0] == RunEvent(type="run_start", run_id="r1")


def test_tool_output_text_unwraps_mcp_content_blocks() -> None:
    from be_agent.workflow.engine import tool_output_text

    assert tool_output_text("그대로") == "그대로"
    assert tool_output_text([{"type": "text", "text": "첫째"}, {"type": "text", "text": "둘째"}]) == "첫째\n\n둘째"
    assert tool_output_text({"a": 1}) == '{"a": 1}'
