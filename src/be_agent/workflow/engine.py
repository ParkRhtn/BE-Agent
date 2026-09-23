"""워크플로우 그래프 검증과 실행.

실행 규칙
- 노드는 위상 정렬 순서로 하나씩 실행한다 (순환 금지).
- 들어오는 엣지 중 하나라도 "활성"이면 실행하고, 모두 비활성이면 건너뛴다.
  엣지는 출발 노드가 실행되었을 때 활성이며, 조건 노드에서는 결과("true"/"false")와 같은 핸들의 엣지만 활성이다.
- 각 노드의 출력은 문자열이며 `{{노드ID}}` 로 참조한다. 사용자 입력은 `{{input}}`.
"""

import json
import re
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from be_agent.agent.service import AgentSpec
from be_agent.schemas.workflows import WorkflowGraph, WorkflowNode

_TEMPLATE = re.compile(r"\{\{\s*([\w-]+)\s*\}\}")
CONDITION_OPERATORS = ("contains", "not_contains", "equals", "not_equals", "is_empty", "not_empty")


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
                errors.append(f"{prefix} 허용되지 않은 모델입니다: {model}")
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


class Runtime(Protocol):
    """엔진이 바깥 세계와 만나는 지점. 테스트에서 바꿔 끼울 수 있다."""

    default_model: str

    def create_model(self, name: str) -> BaseChatModel: ...
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

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class _RunState:
    variables: dict[str, str]
    executed: dict[str, str] = field(default_factory=dict)  # node_id -> 출력
    final_output: str = ""


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


class WorkflowExecutor:
    def __init__(self, graph: WorkflowGraph, runtime: Runtime, agents: dict[str, AgentSpec]) -> None:
        order = topological_order(graph)
        if order is None:
            raise ValueError("순환이 있는 그래프는 실행할 수 없습니다.")
        self._order = order
        self._graph = graph
        self._runtime = runtime
        self._agents = agents

    def _is_active(self, node: WorkflowNode, state: _RunState) -> bool:
        if node.type == "start":
            return True
        by_id = {n.id: n for n in self._graph.nodes}
        for edge in self._graph.edges:
            if edge.target != node.id or edge.source not in state.executed:
                continue
            if by_id[edge.source].type != "condition" or edge.sourceHandle == state.executed[edge.source]:
                return True
        return False

    def _active_input(self, node: WorkflowNode, state: _RunState) -> str:
        """활성 연결로 들어온 출력. 조건 노드는 "true"/"false" 대신 조건 노드가 받은 값을 넘겨준다."""
        by_id = {n.id: n for n in self._graph.nodes}
        for edge in self._graph.edges:
            if edge.target != node.id or edge.source not in state.executed:
                continue
            source = by_id[edge.source]
            if source.type != "condition":
                return state.executed[source.id]
            if edge.sourceHandle == state.executed[source.id]:
                return self._active_input(source, state)
        return ""

    async def run(self, user_input: str) -> AsyncIterator[RunEvent]:
        state = _RunState(variables={"input": user_input})
        yield RunEvent(type="run_start")

        for node in self._order:
            if not self._is_active(node, state):
                yield RunEvent(type="node_skip", node_id=node.id)
                continue

            yield RunEvent(type="node_start", node_id=node.id)
            deltas: list[str] = []
            try:
                async for delta in self._execute(node, state):
                    deltas.append(delta)
                    yield RunEvent(type="node_delta", node_id=node.id, delta=delta)
                output = state.executed[node.id] if node.id in state.executed else "".join(deltas)
            except Exception as exc:  # 노드 하나가 실패하면 실행을 멈춘다
                yield RunEvent(type="node_error", node_id=node.id, error=f"{type(exc).__name__}: {exc}")
                return

            state.executed[node.id] = output
            state.variables[node.id] = output
            yield RunEvent(type="node_finish", node_id=node.id, output=output)

        yield RunEvent(type="run_finish", output=state.final_output)

    async def _execute(self, node: WorkflowNode, state: _RunState) -> AsyncIterator[str]:
        """스트리밍할 텍스트 조각을 내보낸다. 조각 없이 끝나는 노드는 state.executed 에 직접 출력을 넣는다."""
        v = state.variables
        match node.type:
            case "start":
                state.executed[node.id] = v["input"]
            case "llm":
                model = self._runtime.create_model(_text(node, "model") or self._runtime.default_model)
                messages: list[Any] = []
                if system := render(_text(node, "system"), v).strip():
                    messages.append(SystemMessage(content=system))
                messages.append(HumanMessage(content=render(_text(node, "prompt"), v)))
                async for chunk in model.astream(messages):
                    if chunk.text:
                        yield chunk.text
            case "agent":
                spec = self._agents[_text(node, "agent_id")]
                message = render(_text(node, "message", "{{input}}"), v)
                state.executed[node.id] = await self._runtime.run_agent(spec, message)
            case "tool":
                tool = self._runtime.get_tool(_text(node, "tool"))
                if tool is None:
                    raise ValueError(f"도구를 찾을 수 없습니다: {_text(node, 'tool')}")
                args = json.loads(_text(node, "args", "{}").strip() or "{}")
                rendered = {k: render(val, v) if isinstance(val, str) else val for k, val in args.items()}
                result = await tool.ainvoke(rendered)
                state.executed[node.id] = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            case "condition":
                left = render(_text(node, "left", "{{input}}"), v)
                right = render(_text(node, "right"), v)
                passed = evaluate_condition(_text(node, "operator", "contains"), left, right)
                state.executed[node.id] = "true" if passed else "false"
            case "end":
                template = _text(node, "output")
                output = render(template, v) if template.strip() else self._active_input(node, state)
                state.executed[node.id] = output
                state.final_output = output
