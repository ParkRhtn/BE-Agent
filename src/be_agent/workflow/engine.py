"""워크플로우 그래프 검증과 실행.

실행 규칙
- LangGraph StateGraph 로 컴파일해 실행한다 (순환 금지). 서로 무관한 갈래는 동시에 실행된다.
- 들어오는 엣지 중 하나라도 "활성"이면 실행하고, 모두 비활성이면 건너뛴다.
  엣지는 출발 노드가 실행되었을 때 활성이며, 조건 노드에서는 결과("true"/"false")와 같은 핸들의 엣지만 활성이다.
- 각 노드의 출력은 문자열이며 `{{노드ID}}` 로 참조한다. 사용자 입력은 `{{input}}`.
"""

import json
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from be_agent.agent.service import AgentSpec
from be_agent.schemas.workflows import WorkflowGraph, WorkflowNode

_TEMPLATE = re.compile(r"\{\{\s*([\w-]+)\s*\}\}")
CONDITION_OPERATORS = ("contains", "not_contains", "equals", "not_equals", "is_empty", "not_empty")


def _merge(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    return {**left, **right}


def _latest(left: str | None, right: str | None) -> str | None:
    return right if right is not None else left


def render(template: str, variables: dict[str, str]) -> str:
    """`{{name}}` 을 변수 값으로 바꾼다. 없는 변수는 빈 문자열."""
    return _TEMPLATE.sub(lambda m: variables.get(m.group(1), ""), template)


def _text(node: WorkflowNode, key: str, default: str = "") -> str:
    value = node.data.get(key, default)
    return value if isinstance(value, str) else default


# ---------------------------------------------------------------------------
# 검증


@dataclass
class ValidationContext:
    tool_names: set[str]
    agent_ids: set[str]
    allowed_models: set[str]
    has_default_model: bool = True


def topological_order(graph: WorkflowGraph) -> list[WorkflowNode] | None:
    """순환이 있으면 None. 같은 단계에서는 노드 목록 순서를 유지한다."""
    indegree = {n.id: 0 for n in graph.nodes}
    for edge in graph.edges:
        if edge.target in indegree:
            indegree[edge.target] += 1
    ready = [n for n in graph.nodes if indegree[n.id] == 0]
    order: list[WorkflowNode] = []
    by_id = {n.id: n for n in graph.nodes}
    while ready:
        node = ready.pop(0)
        order.append(node)
        for edge in graph.edges:
            if edge.source == node.id and edge.target in indegree:
                indegree[edge.target] -= 1
                if indegree[edge.target] == 0:
                    ready.append(by_id[edge.target])
    return order if len(order) == len(graph.nodes) else None


def validate_graph(graph: WorkflowGraph, ctx: ValidationContext) -> list[str]:
    errors: list[str] = []
    ids = [n.id for n in graph.nodes]
    if len(ids) != len(set(ids)):
        errors.append("노드 ID 가 중복되었습니다.")
    by_id = {n.id: n for n in graph.nodes}

    starts = [n for n in graph.nodes if n.type == "start"]
    if len(starts) != 1:
        errors.append("시작 노드는 정확히 1개여야 합니다.")
    if not any(n.type == "end" for n in graph.nodes):
        errors.append("종료 노드가 1개 이상 필요합니다.")

    for edge in graph.edges:
        source, target = by_id.get(edge.source), by_id.get(edge.target)
        if source is None or target is None:
            errors.append(f"존재하지 않는 노드를 잇는 연결이 있습니다: {edge.source} → {edge.target}")
            continue
        if target.type == "start":
            errors.append("시작 노드로 들어오는 연결은 둘 수 없습니다.")
        if source.type == "end":
            errors.append(f"[{source.id}] 종료 노드에서 나가는 연결은 둘 수 없습니다.")
        if source.type == "condition" and edge.sourceHandle not in ("true", "false"):
            errors.append(f"[{source.id}] 조건 노드의 연결은 참/거짓 핸들에서 시작해야 합니다.")

    if topological_order(graph) is None:
        errors.append("연결에 순환이 있습니다. 워크플로우는 한 방향으로만 흘러야 합니다.")

    for node in graph.nodes:
        prefix = f"[{node.id}]"
        if node.type == "llm":
            if not _text(node, "prompt").strip():
                errors.append(f"{prefix} 프롬프트를 입력하세요.")
            model = _text(node, "model")
            if model and model not in ctx.allowed_models:
                errors.append(f"{prefix} 사용할 수 없는 모델입니다. 설정에서 켠 모델을 고르세요.")
            elif not model and not ctx.has_default_model:
                errors.append(f"{prefix} 사용할 모델이 없습니다. 설정에서 모델 제공사를 추가하세요.")
        elif node.type == "agent":
            if _text(node, "agent_id") not in ctx.agent_ids:
                errors.append(f"{prefix} 에이전트를 선택하세요.")
        elif node.type == "tool":
            if _text(node, "tool") not in ctx.tool_names:
                errors.append(f"{prefix} 도구를 선택하세요.")
            args = _text(node, "args", "{}").strip() or "{}"
            try:
                if not isinstance(json.loads(args), dict):
                    raise ValueError
            except ValueError:
                errors.append(f'{prefix} 도구 인자는 JSON 객체여야 합니다. 예: {{"timezone": "UTC"}}')
        elif node.type == "condition" and _text(node, "operator", "contains") not in CONDITION_OPERATORS:
            errors.append(f"{prefix} 알 수 없는 조건입니다.")
    return errors


# ---------------------------------------------------------------------------
# 실행


class NodeTrace(Protocol):
    def finish(self, *, output: Any = None, error: str | None = None) -> None: ...


class Runtime(Protocol):
    """엔진이 바깥 세계와 만나는 지점. 테스트에서 바꿔 끼울 수 있다."""

    # 모델·도구 호출에 넘길 LangChain 콜백 (추적용). 없으면 빈 목록.
    callbacks: list[Any]

    def trace_node(self, node_id: str, kind: str, input: Any) -> AbstractContextManager[NodeTrace]:
        """노드 하나의 추적 기록. 안에서 일어난 모델·도구 호출이 이 기록 아래에 붙는다."""
        ...

    def create_model(self, model_id: str | None) -> BaseChatModel:
        """None 이면 기본 모델."""
        ...

    def get_tool(self, name: str) -> BaseTool | None: ...
    def run_agent(self, spec: AgentSpec, message: str) -> Awaitable[str]: ...


EventType = Literal["run_start", "node_start", "node_delta", "node_finish", "node_skip", "node_error", "run_finish"]


@dataclass
class RunEvent:
    type: EventType
    node_id: str | None = None
    delta: str | None = None
    output: str | None = None
    error: str | None = None
    run_id: str | None = None  # run_start 에만: 평가를 남길 때 쓰는 실행 ID

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class _GraphState(TypedDict):
    """LangGraph 상태. 같은 단계에서 병렬로 끝난 노드들의 결과는 합쳐진다."""

    variables: Annotated[dict[str, str], _merge]  # {{이름}} 으로 쓸 값: input + 실행된 노드 출력
    executed: Annotated[dict[str, str], _merge]  # 실행된 노드 → 출력 (조건 노드는 "true"/"false")
    final_output: Annotated[str | None, _latest]


class _NodeFailed(Exception):
    """노드 하나가 실패하면 실행 전체를 멈춘다 (node_error 이벤트는 이미 보냈다)."""


def evaluate_condition(operator: str, left: str, right: str) -> bool:
    match operator:
        case "contains":
            return right in left
        case "not_contains":
            return right not in left
        case "equals":
            return left.strip() == right.strip()
        case "not_equals":
            return left.strip() != right.strip()
        case "is_empty":
            return not left.strip()
        case "not_empty":
            return bool(left.strip())
    raise ValueError(f"알 수 없는 조건: {operator}")


def tool_output_text(output: Any) -> str:
    """도구 결과를 다음 노드가 읽을 글로. MCP 도구는 [{"type": "text", "text": ...}] 조각 목록을 돌려준다."""
    if isinstance(output, str):
        return output
    if isinstance(output, list) and output and all(isinstance(p, dict) and "text" in p for p in output):
        return "\n\n".join(str(p["text"]) for p in output)
    return json.dumps(output, ensure_ascii=False)


def _trace_input(node: WorkflowNode, variables: dict[str, str]) -> Any:
    """추적 기록에 남길 노드 입력: 변수를 채운 설정값."""
    if node.type == "start":
        return variables["input"]
    return {k: render(v, variables) if isinstance(v, str) else v for k, v in node.data.items()}


class WorkflowExecutor:
    """캔버스 그래프를 LangGraph StateGraph 로 컴파일해 실행한다.

    - 캔버스 노드 하나 = LangGraph 노드 하나, 연결선 = 엣지. 앞 노드가 여럿이면 모두 끝난 뒤 한 번 실행한다.
    - 모든 노드는 자기 차례에 실행되고, 활성 연결이 없으면 "건너뜀"만 남긴다.
      (조건으로 한쪽 갈래가 건너뛰어져도 합류 노드가 기다리다 멈추지 않게)
    - 서로 무관한 갈래는 같은 단계에서 동시에 실행된다.
    - 노드 이벤트는 LangGraph custom 스트림으로 내보낸다.
    """

    def __init__(self, graph: WorkflowGraph, runtime: Runtime, agents: dict[str, AgentSpec]) -> None:
        if topological_order(graph) is None:
            raise ValueError("순환이 있는 그래프는 실행할 수 없습니다.")
        self._graph = graph
        self._runtime = runtime
        self._agents = agents
        self._types = {n.id: n.type for n in graph.nodes}
        self._compiled = self._build()

    def _build(self) -> Any:
        builder = StateGraph(_GraphState)
        for node in self._graph.nodes:
            builder.add_node(node.id, self._node_fn(node))

        predecessors: dict[str, list[str]] = {n.id: [] for n in self._graph.nodes}
        has_next: set[str] = set()
        for edge in self._graph.edges:
            if edge.source not in predecessors[edge.target]:  # 참·거짓이 같은 노드로 가도 한 번만
                predecessors[edge.target].append(edge.source)
            has_next.add(edge.source)

        for node_id, sources in predecessors.items():
            if not sources:
                builder.add_edge(START, node_id)  # 시작 노드 (그리고 아무 데서도 오지 않는 노드는 바로 건너뜀)
            elif len(sources) == 1:
                builder.add_edge(sources[0], node_id)
            else:
                builder.add_edge(sources, node_id)  # 앞 노드가 모두 끝난 뒤 한 번
            if node_id not in has_next:
                builder.add_edge(node_id, END)
        return builder.compile()

    def _is_active(self, node: WorkflowNode, executed: dict[str, str]) -> bool:
        if node.type == "start":
            return True
        for edge in self._graph.edges:
            if edge.target != node.id or edge.source not in executed:
                continue
            if self._types[edge.source] != "condition" or edge.sourceHandle == executed[edge.source]:
                return True
        return False

    def _active_input(self, node_id: str, executed: dict[str, str]) -> str:
        """활성 연결로 들어온 출력. 조건 노드는 "true"/"false" 대신 조건 노드가 받은 값을 넘겨준다."""
        for edge in self._graph.edges:
            if edge.target != node_id or edge.source not in executed:
                continue
            if self._types[edge.source] != "condition":
                return executed[edge.source]
            if edge.sourceHandle == executed[edge.source]:
                return self._active_input(edge.source, executed)
        return ""

    def _node_fn(self, node: WorkflowNode) -> Any:
        async def run_node(state: _GraphState) -> dict[str, Any]:
            write = get_stream_writer()
            if not self._is_active(node, state["executed"]):
                write(RunEvent(type="node_skip", node_id=node.id).to_dict())
                return {}

            write(RunEvent(type="node_start", node_id=node.id).to_dict())
            deltas: list[str] = []
            result: dict[str, str] = {}
            with self._runtime.trace_node(node.id, node.type, _trace_input(node, state["variables"])) as trace:
                try:
                    async for delta in self._execute(node, state, result):
                        deltas.append(delta)
                        write(RunEvent(type="node_delta", node_id=node.id, delta=delta).to_dict())
                    output = result.get("output", "".join(deltas))
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    trace.finish(error=error)
                    write(RunEvent(type="node_error", node_id=node.id, error=error).to_dict())
                    raise _NodeFailed(error) from exc
                trace.finish(output=output)

            write(RunEvent(type="node_finish", node_id=node.id, output=output).to_dict())
            update: dict[str, Any] = {"executed": {node.id: output}, "variables": {node.id: output}}
            if node.type == "end":
                update["final_output"] = output
            return update

        return run_node

    async def run(self, user_input: str, *, run_id: str | None = None) -> AsyncIterator[RunEvent]:
        yield RunEvent(type="run_start", run_id=run_id)
        initial: _GraphState = {"variables": {"input": user_input}, "executed": {}, "final_output": None}
        final_output: str | None = None
        try:
            async for mode, chunk in self._compiled.astream(
                initial,
                # 단계 수 제한: 노드 수만큼이면 충분하다 (순환은 검증에서 막는다)
                config={"recursion_limit": len(self._graph.nodes) + 10},
                stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    yield RunEvent(**chunk)
                else:
                    final_output = chunk.get("final_output")
        except _NodeFailed:
            return  # 실패한 노드가 node_error 를 이미 보냈다
        yield RunEvent(type="run_finish", output=final_output or "")

    def _call_config(self, node: WorkflowNode) -> RunnableConfig:
        return RunnableConfig(callbacks=self._runtime.callbacks, run_name=node.id)

    async def _execute(self, node: WorkflowNode, state: _GraphState, result: dict[str, str]) -> AsyncIterator[str]:
        """스트리밍할 텍스트 조각을 내보낸다. 조각 없이 끝나는 노드는 result["output"] 에 출력을 넣는다."""
        v = state["variables"]
        match node.type:
            case "start":
                result["output"] = v["input"]
            case "llm":
                model = self._runtime.create_model(_text(node, "model") or None)
                messages: list[Any] = []
                if system := render(_text(node, "system"), v).strip():
                    messages.append(SystemMessage(content=system))
                messages.append(HumanMessage(content=render(_text(node, "prompt"), v)))
                async for chunk in model.astream(messages, config=self._call_config(node)):
                    if chunk.text:
                        yield chunk.text
            case "agent":
                spec = self._agents[_text(node, "agent_id")]
                message = render(_text(node, "message", "{{input}}"), v)
                result["output"] = await self._runtime.run_agent(spec, message)
            case "tool":
                tool = self._runtime.get_tool(_text(node, "tool"))
                if tool is None:
                    raise ValueError(f"도구를 찾을 수 없습니다: {_text(node, 'tool')}")
                args = json.loads(_text(node, "args", "{}").strip() or "{}")
                rendered = {k: render(val, v) if isinstance(val, str) else val for k, val in args.items()}
                output = await tool.ainvoke(rendered, config=self._call_config(node))
                result["output"] = tool_output_text(output)
            case "condition":
                left = render(_text(node, "left", "{{input}}"), v)
                right = render(_text(node, "right"), v)
                passed = evaluate_condition(_text(node, "operator", "contains"), left, right)
                result["output"] = "true" if passed else "false"
            case "end":
                template = _text(node, "output")
                result["output"] = (
                    render(template, v) if template.strip() else self._active_input(node.id, state["executed"])
                )
