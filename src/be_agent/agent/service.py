import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from be_agent.agent.stream import to_agent_events
from be_agent.core.llm import ModelConfig, create_chat_model
from be_agent.streaming.events import AgentEvent, RunError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    """에이전트 그래프를 결정하는 설정. 같은 설정이면 컴파일된 그래프를 재사용한다."""

    model: ModelConfig
    system_prompt: str | None = None  # None 이면 서비스 기본 프롬프트
    tools: tuple[str, ...] | None = None  # None 이면 등록된 모든 도구


class AgentService:
    """에이전트 설정별 그래프를 만들어 캐시하고, 스레드 단위로 실행한다."""

    def __init__(
        self,
        *,
        checkpointer: BaseCheckpointSaver,
        tools: Sequence[BaseTool],
        system_prompt: str,
        callbacks: Sequence[BaseCallbackHandler] = (),
        model_factory: Callable[[ModelConfig], BaseChatModel] = create_chat_model,
    ) -> None:
        self.checkpointer = checkpointer
        self._tools = {t.name: t for t in tools}
        self._system_prompt = system_prompt
        self._callbacks = list(callbacks)
        self._model_factory = model_factory
        self._agents: dict[AgentSpec, CompiledStateGraph[Any, Any, Any, Any]] = {}

    @property
    def tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def _get_agent(self, spec: AgentSpec) -> CompiledStateGraph[Any, Any, Any, Any]:
        if spec not in self._agents:
            names = self._tools.keys() if spec.tools is None else spec.tools
            self._agents[spec] = create_agent(
                model=self._model_factory(spec.model),
                tools=[self._tools[n] for n in names if n in self._tools],  # 없어진 도구는 무시
                system_prompt=spec.system_prompt or self._system_prompt,
                checkpointer=self.checkpointer,
            )
        return self._agents[spec]

    def _config(
        self, thread_id: str, *, run_name: str | None = None, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}, "callbacks": self._callbacks}
        if run_name:
            config["run_name"] = run_name
        if metadata:
            config["metadata"] = metadata
        return config

    async def stream(
        self,
        *,
        thread_id: str,
        message: str,
        spec: AgentSpec,
        run_name: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        try:
            agent = self._get_agent(spec)
            stream = agent.astream(
                {"messages": [HumanMessage(content=message, id=message_id)]},
                self._config(thread_id, run_name=run_name, metadata=trace_metadata),  # type: ignore[arg-type]
                stream_mode=["messages", "updates"],
            )
            async for event in to_agent_events(stream):
                yield event
        except Exception as exc:
            logger.exception("Agent run failed (thread=%s, model=%s)", thread_id, spec.model.model)
            yield RunError(message=f"{type(exc).__name__}: {exc}")

    def create_model(self, config: ModelConfig) -> BaseChatModel:
        return self._model_factory(config)

    def get_tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    async def run_once(self, *, spec: AgentSpec, message: str, run_id: str, run_name: str | None = None) -> str:
        """대화 이력을 남기지 않고 에이전트를 한 번 실행해 최종 답변만 돌려준다 (워크플로우용)."""
        thread_id = f"workflow-run-{run_id}"
        try:
            result = await self._get_agent(spec).ainvoke(
                {"messages": [HumanMessage(content=message)]},
                self._config(thread_id, run_name=run_name),  # type: ignore[arg-type]
            )
            return result["messages"][-1].text
        finally:
            await self.checkpointer.adelete_thread(thread_id)

    async def get_messages(self, thread_id: str) -> list[BaseMessage]:
        checkpoint = await self.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        if checkpoint is None:
            return []
        return list(checkpoint.checkpoint["channel_values"].get("messages", []))

    async def delete_thread(self, thread_id: str) -> None:
        await self.checkpointer.adelete_thread(thread_id)
