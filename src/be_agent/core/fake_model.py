"""API 키 없이 에이전트 흐름(스트리밍, 도구 호출)을 확인하기 위한 개발용 모델."""

import json
import re
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

_TIME_KEYWORDS = ("시간", "몇 시", "time")


class FakeToolChatModel(BaseChatModel):
    """
    - 마지막 메시지가 도구 결과면: 결과를 요약해 답한다.
    - 사용자가 시간을 물으면: get_current_time 도구를 호출한다.
    - 그 외: 입력을 그대로 되돌려준다.

    토큰 수는 단어 수로 흉내 낸다 (사용량 기록 확인용).
    """

    model_name: str = "echo"

    @staticmethod
    def _usage(messages: list[BaseMessage], reply: AIMessage) -> UsageMetadata:
        input_tokens = sum(len(m.text.split()) for m in messages)
        output_tokens = len(reply.text.split()) + len(reply.tool_calls)
        return UsageMetadata(
            input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens
        )

    @property
    def _llm_type(self) -> str:
        return "fake-tool-chat-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "FakeToolChatModel":  # type: ignore[override]
        return self

    def _respond(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            return AIMessage(content=f"도구 실행 결과입니다: {last.text}")
        if any(k in last.text.lower() for k in _TIME_KEYWORDS):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_current_time",
                        "args": {"timezone": "Asia/Seoul"},
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                    }
                ],
            )
        return AIMessage(content=f"(fake 모델) 입력하신 내용: {last.text}")

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._respond(messages)
        message.usage_metadata = self._usage(messages, message)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._respond(messages)
        message_id = f"run-{uuid.uuid4()}"
        for token in re.split(r"(\s)", message.text):
            if not token:
                continue
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=token, id=message_id))
            if run_manager:
                run_manager.on_llm_new_token(token, chunk=chunk)
            yield chunk
        for index, call in enumerate(message.tool_calls):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id=message_id,
                    tool_call_chunks=[
                        tool_call_chunk(
                            name=call["name"],
                            args=json.dumps(call["args"]),
                            id=call["id"],
                            index=index,
                        )
                    ],
                )
            )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", id=message_id, usage_metadata=self._usage(messages, message))
        )
