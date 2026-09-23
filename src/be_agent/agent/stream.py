"""LangGraph 스트림(messages + updates 모드)을 내부 AgentEvent 로 변환한다."""

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from be_agent.streaming.events import (
    AgentEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallReady,
    ToolCallStart,
    ToolResult,
)


async def to_agent_events(stream: AsyncIterator[Any]) -> AsyncIterator[AgentEvent]:
    # (message_id, tool_call_chunk index) -> tool_call_id. 이어지는 청크에는 id 가 비어 있다.
    call_ids_by_index: dict[tuple[str | None, int | None], str] = {}

    async for mode, data in stream:
        if mode == "messages":
            message, _metadata = data
            if not isinstance(message, AIMessage):
                continue  # 도구 결과는 updates 모드에서 처리
            if message.text:
                yield TextDelta(message_id=message.id or "text", delta=message.text)
            if isinstance(message, AIMessageChunk):
                for chunk in message.tool_call_chunks:
                    key = (message.id, chunk.get("index"))
                    if chunk.get("id") and chunk.get("name"):
                        call_ids_by_index[key] = chunk["id"]  # type: ignore[assignment]
                        yield ToolCallStart(tool_call_id=chunk["id"], tool_name=chunk["name"])  # type: ignore[arg-type]
                    call_id = call_ids_by_index.get(key)
                    if call_id and chunk.get("args"):
                        yield ToolCallArgsDelta(tool_call_id=call_id, delta=chunk["args"])  # type: ignore[arg-type]

        elif mode == "updates":
            for message in _messages_from_updates(data):
                if isinstance(message, AIMessage):
                    for call in message.tool_calls:
                        if call["id"]:
                            yield ToolCallReady(tool_call_id=call["id"], tool_name=call["name"], input=call["args"])
                elif isinstance(message, ToolMessage):
                    yield ToolResult(
                        tool_call_id=message.tool_call_id,
                        output=message.content,
                        is_error=message.status == "error",
                    )


def _messages_from_updates(data: Any) -> list[Any]:
    # updates 는 {노드이름: 업데이트} 이며, 도구를 병렬 실행하면 업데이트가 리스트로 온다.
    messages: list[Any] = []
    if not isinstance(data, dict):
        return messages
    for update in data.values():
        for item in update if isinstance(update, list) else [update]:
            if isinstance(item, dict) and isinstance(item.get("messages"), list):
                messages.extend(item["messages"])
    return messages
