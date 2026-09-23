"""Vercel AI SDK UI Message Stream 프로토콜 어댑터 (useChat 과 호환).

https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from be_agent.streaming.events import (
    AgentEvent,
    RunError,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallReady,
    ToolCallStart,
    ToolResult,
)

AI_SDK_HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # nginx 등 프록시 버퍼링 방지
}


def _sse(payload: dict[str, Any] | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n"


# 사용자 메시지 ID 에 실행 ID 를 심어, 이력을 다시 불러와도 답변 ID·실행 ID 가 스트리밍 때와 같게 한다
USER_MESSAGE_PREFIX = "user-"
ASSISTANT_MESSAGE_PREFIX = "msg-"


def run_message_ids(run_id: str) -> tuple[str, str]:
    """(사용자 메시지 ID, 답변 메시지 ID)"""
    return f"{USER_MESSAGE_PREFIX}{run_id}", f"{ASSISTANT_MESSAGE_PREFIX}{run_id}"


async def encode_ai_sdk_stream(
    events: AsyncIterator[AgentEvent], *, message_id: str | None = None, metadata: dict[str, Any] | None = None
) -> AsyncIterator[str]:
    """내부 이벤트를 AI SDK SSE 청크로 변환한다. 한 번의 실행 = 하나의 assistant UIMessage."""
    open_text_id: str | None = None
    step_open = False
    after_tool_results = False
    started_tool_calls: set[str] = set()

    def close_text() -> list[str]:
        nonlocal open_text_id
        if open_text_id is None:
            return []
        chunk = _sse({"type": "text-end", "id": open_text_id})
        open_text_id = None
        return [chunk]

    def ensure_step() -> list[str]:
        # 도구 결과 뒤에 오는 모델 출력은 새 step (= 새 LLM 호출) 이다.
        nonlocal step_open, after_tool_results
        out: list[str] = []
        if step_open and after_tool_results:
            out += close_text()
            out.append(_sse({"type": "finish-step"}))
            step_open = False
        if not step_open:
            out.append(_sse({"type": "start-step"}))
            step_open = True
        after_tool_results = False
        return out

    start: dict[str, Any] = {"type": "start", "messageId": message_id or f"msg-{uuid.uuid4()}"}
    if metadata:
        start["messageMetadata"] = metadata
    yield _sse(start)

    async for event in events:
        match event:
            case TextDelta(message_id=message_id, delta=delta):
                for chunk in ensure_step():
                    yield chunk
                if open_text_id != message_id:
                    for chunk in close_text():
                        yield chunk
                    open_text_id = message_id
                    yield _sse({"type": "text-start", "id": message_id})
                yield _sse({"type": "text-delta", "id": message_id, "delta": delta})

            case ToolCallStart(tool_call_id=call_id, tool_name=name):
                for chunk in [*ensure_step(), *close_text()]:
                    yield chunk
                started_tool_calls.add(call_id)
                yield _sse({"type": "tool-input-start", "toolCallId": call_id, "toolName": name})

            case ToolCallArgsDelta(tool_call_id=call_id, delta=delta):
                yield _sse({"type": "tool-input-delta", "toolCallId": call_id, "inputTextDelta": delta})

            case ToolCallReady(tool_call_id=call_id, tool_name=name, input=tool_input):
                for chunk in [*ensure_step(), *close_text()]:
                    yield chunk
                if call_id not in started_tool_calls:
                    started_tool_calls.add(call_id)
                    yield _sse({"type": "tool-input-start", "toolCallId": call_id, "toolName": name})
                yield _sse(
                    {"type": "tool-input-available", "toolCallId": call_id, "toolName": name, "input": tool_input}
                )

            case ToolResult(tool_call_id=call_id, output=output, is_error=is_error):
                if is_error:
                    yield _sse({"type": "tool-output-error", "toolCallId": call_id, "errorText": str(output)})
                else:
                    yield _sse({"type": "tool-output-available", "toolCallId": call_id, "output": output})
                after_tool_results = True

            case RunError(message=message):
                for chunk in close_text():
                    yield chunk
                yield _sse({"type": "error", "errorText": message})

    for chunk in close_text():
        yield chunk
    if step_open:
        yield _sse({"type": "finish-step"})
    yield _sse({"type": "finish"})
    yield _sse("[DONE]")


def to_ui_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """LangGraph 대화 이력을 AI SDK UIMessage 목록으로 변환한다 (스트림 결과와 같은 모양)."""
    ui_messages: list[dict[str, Any]] = []
    assistant: dict[str, Any] | None = None
    tool_parts: dict[str, dict[str, Any]] = {}

    run_id: str | None = None  # 직전 사용자 메시지에 심어 둔 실행 ID
    for message in messages:
        if isinstance(message, HumanMessage):
            assistant = None
            run_id = (
                message.id.removeprefix(USER_MESSAGE_PREFIX)
                if message.id and message.id.startswith(USER_MESSAGE_PREFIX)
                else None
            )
            ui_messages.append(
                {
                    "id": message.id or f"msg-{uuid.uuid4()}",
                    "role": "user",
                    "parts": [{"type": "text", "text": message.text}],
                }
            )
        elif isinstance(message, AIMessage):
            if assistant is None:
                if run_id:
                    assistant = {
                        "id": run_message_ids(run_id)[1],
                        "role": "assistant",
                        "parts": [],
                        "metadata": {"runId": run_id},
                    }
                else:  # 실행 ID 를 쓰기 전의 옛 대화
                    assistant = {"id": message.id or f"msg-{uuid.uuid4()}", "role": "assistant", "parts": []}
                ui_messages.append(assistant)
            parts = assistant["parts"]
            parts.append({"type": "step-start"})
            if message.text:
                parts.append({"type": "text", "text": message.text, "state": "done"})
            for call in message.tool_calls:
                part = {
                    "type": f"tool-{call['name']}",
                    "toolCallId": call["id"],
                    "state": "input-available",
                    "input": call["args"],
                }
                if call["id"]:
                    tool_parts[call["id"]] = part
                parts.append(part)
        elif isinstance(message, ToolMessage):
            part = tool_parts.get(message.tool_call_id)
            if part is None:
                continue
            if message.status == "error":
                part.update(state="output-error", errorText=message.text)
            else:
                part.update(state="output-available", output=message.content)

    return ui_messages
