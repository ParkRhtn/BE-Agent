"""Langfuse 추적. 키가 없으면 모든 기능이 아무 일도 하지 않는다.

- 대화: LangChain 콜백 + metadata(langfuse_user_id / langfuse_session_id / langfuse_tags)로 사용자·대화별로 묶는다.
- 워크플로우: 실행 한 번을 부모 기록(span) 하나로 열고, 노드마다 하위 기록을 단다.
  노드 안의 모델·도구 호출은 LangChain 콜백이 현재 기록 아래에 자동으로 붙는다 (OpenTelemetry 문맥).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.callbacks import BaseCallbackHandler

from be_agent.core.config import Settings

logger = logging.getLogger(__name__)

SpanKind = Literal["span", "agent", "tool", "chain"]


class SpanHandle:
    """기록 하나. Langfuse 가 꺼져 있으면 아무 일도 하지 않는다."""

    def __init__(self, observation: Any = None) -> None:
        self._observation = observation

    def finish(self, *, output: Any = None, error: str | None = None) -> None:
        if self._observation is None:
            return
        if error is not None:
            self._observation.update(level="ERROR", status_message=error)
        else:
            self._observation.update(output=output)


@dataclass
class Tracing:
    client: Any = None  # langfuse.Langfuse
    callbacks: list[BaseCallbackHandler] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @staticmethod
    def chat_metadata(*, user_id: str, session_id: str, tags: list[str]) -> dict[str, Any]:
        """LangChain 호출 metadata 에 넣으면 Langfuse 가 사용자·세션·태그로 묶는다."""
        return {"langfuse_user_id": user_id, "langfuse_session_id": session_id, "langfuse_tags": tags}

    @staticmethod
    def trace_id_for(run_id: str) -> str:
        """실행 ID 로 정해지는 Langfuse 기록 ID. 평가 점수를 나중에 같은 기록에 붙일 수 있다."""
        from langfuse import Langfuse

        return Langfuse.create_trace_id(seed=run_id)

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        user_id: str,
        session_id: str,
        tags: list[str],
        input: Any = None,
        trace_id: str | None = None,
    ) -> Iterator[SpanHandle]:
        """부모 기록. 안에서 만든 기록과 LangChain 호출은 모두 이 기록 아래에 붙고 사용자·세션이 같이 붙는다."""
        if not self.enabled:
            yield SpanHandle()
            return
        from langfuse import propagate_attributes

        trace_context = {"trace_id": trace_id} if trace_id else None
        with (
            self.client.start_as_current_observation(
                name=name, as_type="chain", input=input, trace_context=trace_context
            ) as root,
            propagate_attributes(user_id=user_id, session_id=session_id, tags=tags, trace_name=name),
        ):
            yield SpanHandle(root)

    @contextmanager
    def span(self, name: str, *, kind: SpanKind = "span", input: Any = None) -> Iterator[SpanHandle]:
        if not self.enabled:
            yield SpanHandle()
            return
        with self.client.start_as_current_observation(name=name, as_type=kind, input=input) as observation:
            yield SpanHandle(observation)

    def score_feedback(self, *, trace_id: str, value: int, comment: str | None) -> None:
        """사용자 평가를 기록에 점수로 붙인다. 같은 기록에 다시 보내면 덮어쓴다."""
        if not self.enabled:
            return
        self.client.create_score(
            name="user-feedback",
            value=float(value),
            data_type="NUMERIC",
            trace_id=trace_id,
            score_id=f"{trace_id}-user-feedback",
            comment=comment,
        )

    def shutdown(self) -> None:
        if self.enabled:
            self.client.shutdown()  # 남은 기록을 보내고 닫는다


def create_tracing(settings: Settings, *, span_exporter: Any = None) -> Tracing:
    """Langfuse 키가 설정되어 있으면 추적을 켠다. span_exporter 는 테스트에서 기록을 메모리로 받을 때 쓴다."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return Tracing()

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        environment=settings.environment,
        span_exporter=span_exporter,
    )
    logger.info("Langfuse tracing enabled")
    return Tracing(client=client, callbacks=[CallbackHandler(public_key=settings.langfuse_public_key)])
