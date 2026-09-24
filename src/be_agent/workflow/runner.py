"""워크플로우 실행 한 번: 검증 → 실행 기록(runs) 생성 → 노드 실행 이벤트 → 결과·사용량 저장.

화면에서 누른 실행(API, SSE 로 이벤트를 흘려보냄)과 예약 실행(스케줄러, 이벤트를 버림)이 같이 쓴다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from be_agent.agent.service import AgentService, AgentSpec
from be_agent.core.context import current_user_id
from be_agent.core.model_registry import ModelNotAvailable, ModelRegistry
from be_agent.core.observability import SpanHandle, SpanKind, Tracing
from be_agent.core.usage import collect_usage, save_usage
from be_agent.db.models import Agent, Run, User, Workflow
from be_agent.schemas.workflows import WorkflowGraph
from be_agent.workflow.engine import RunEvent, ValidationContext, WorkflowExecutor, validate_graph

Trigger = Literal["manual", "schedule"]

# 노드 종류 → Langfuse 기록 종류
_NODE_SPAN_KIND: dict[str, SpanKind] = {"agent": "agent", "tool": "tool"}


class WorkflowInvalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class _RunRecord:
    """실행 이벤트를 모아 실행 기록(runs)에 저장할 모양으로 만든다."""

    status: str = "running"
    output: str | None = None
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, event: RunEvent) -> None:
        match event.type:
            case "node_start":
                self.steps.append({"node_id": event.node_id, "status": "running"})
            case "node_skip":
                self.steps.append({"node_id": event.node_id, "status": "skipped"})
            case "node_finish":
                self._step(event.node_id).update(status="done", output=event.output)
            case "node_error":
                self._step(event.node_id).update(status="error", error=event.error)
                self.status, self.error = "error", f"{event.node_id}: {event.error}"
            case "run_finish":
                self.status, self.output = "done", event.output

    def _step(self, node_id: str | None) -> dict[str, Any]:
        for step in reversed(self.steps):
            if step["node_id"] == node_id:
                return step
        step = {"node_id": node_id, "status": "running"}
        self.steps.append(step)
        return step


class _ServiceRuntime:
    """AgentService 를 워크플로우 엔진의 Runtime 으로 감싼다."""

    def __init__(self, service: AgentService, registry: ModelRegistry, tracing: Tracing) -> None:
        self._service = service
        self._registry = registry
        self._tracing = tracing
        self._run_id = uuid.uuid4().hex
        self.callbacks: list[Any] = service.callbacks

    @contextmanager
    def trace_node(self, node_id: str, kind: str, input: Any) -> Iterator[SpanHandle]:
        with self._tracing.span(f"{node_id} ({kind})", kind=_NODE_SPAN_KIND.get(kind, "span"), input=input) as span:
            yield span

    def create_model(self, model_id: str | None) -> BaseChatModel:
        return self._service.create_model(self._registry.resolve(model_id))

    def get_tool(self, name: str) -> BaseTool | None:
        return self._service.get_tool(name)

    async def run_agent(self, spec: AgentSpec, message: str) -> str:
        return await self._service.run_once(
            spec=spec, message=message, run_id=f"{self._run_id}-{uuid.uuid4().hex}", run_name="에이전트"
        )


@dataclass
class WorkflowRunner:
    service: AgentService
    tracing: Tracing
    sessionmaker: async_sessionmaker

    async def start(
        self,
        session: AsyncSession,
        *,
        user: User,
        workflow: Workflow,
        registry: ModelRegistry,
        input: str,
        trigger: Trigger,
    ) -> tuple[str, Callable[[], AsyncIterator[RunEvent]]]:
        """검증하고 실행 기록을 만든다. 돌려준 함수를 부르면 실행이 시작되어 이벤트가 나온다.

        검증에 실패하면 WorkflowInvalid.
        """
        graph = WorkflowGraph.model_validate(workflow.graph)
        agents: dict[str, AgentSpec] = {}
        for a in await session.scalars(select(Agent).where(Agent.user_id == user.id)):
            try:
                _, config = registry.resolve_or_default(a.model)
            except ModelNotAvailable:
                continue  # 쓸 모델이 없는 에이전트는 검증에서 "에이전트를 선택하세요" 로 걸린다
            agents[a.id] = AgentSpec(model=config, system_prompt=a.system_prompt, tools=tuple(a.tools))
        ctx = ValidationContext(
            tool_names={t.name for t in self.service.tools},
            agent_ids=set(agents),
            allowed_models=set(registry.configs),
            has_default_model=registry.default_id is not None,
        )
        if errors := validate_graph(graph, ctx):
            raise WorkflowInvalid(errors)

        executor = WorkflowExecutor(graph, _ServiceRuntime(self.service, registry, self.tracing), agents)
        run_id = uuid.uuid4().hex
        run = Run(
            id=run_id,
            user_id=user.id,
            kind="workflow",
            trigger=trigger,
            workflow_id=workflow.id,
            trace_id=Tracing.trace_id_for(run_id),
            status="running",
            input=input,
        )
        session.add(run)
        await session.commit()

        trace_name = f"워크플로우: {workflow.name}"
        user_id, workflow_id, trace_id = user.id, workflow.id, run.trace_id
        tags = ["workflow", *(["schedule"] if trigger == "schedule" else [])]

        async def save(record: _RunRecord) -> None:
            # 요청 세션은 이미 닫혔을 수 있어 새 세션으로 저장한다
            async with self.sessionmaker() as db:
                saved = await db.get(Run, run_id)
                if saved is not None:
                    saved.status, saved.output, saved.error = record.status, record.output, record.error
                    saved.steps = record.steps
                    saved.finished_at = datetime.now(UTC)
                    await db.commit()

        async def events() -> AsyncIterator[RunEvent]:
            record = _RunRecord()
            current_user_id.set(user_id)  # 사용자별 설정이 필요한 도구(텔레그램 등)용
            with collect_usage() as usage:
                try:
                    # 실행 한 번 = Langfuse 기록 하나. 노드마다 하위 기록이 붙는다.
                    with self.tracing.trace(
                        trace_name,
                        user_id=user_id,
                        session_id=f"workflow-{workflow_id}",
                        tags=tags,
                        input=input,
                        trace_id=trace_id,
                    ) as root:
                        async for event in executor.run(input, run_id=run_id):
                            record.apply(event)
                            if event.type == "run_finish":
                                root.finish(output=event.output)
                            elif event.type == "node_error":
                                root.finish(error=f"{event.node_id}: {event.error}")
                            yield event
                except asyncio.CancelledError:
                    record.status = "cancelled"  # 브라우저에서 중지
                    raise
                except Exception as exc:
                    record.status, record.error = "error", f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    if record.status == "running":
                        record.status = "cancelled"
                    # 취소 중에도 저장은 끝까지 한다. 중간에 멈춰도 이미 부른 모델 호출은 사용량에 남긴다.
                    await asyncio.shield(save(record))
                    await asyncio.shield(
                        save_usage(self.sessionmaker, usage, user_id=user_id, run_id=run_id, source=trace_name)
                    )

        return run_id, events
