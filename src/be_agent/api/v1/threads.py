import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from be_agent.agent.service import AgentSpec
from be_agent.api.deps import AgentServiceDep, CurrentUserDep, ModelRegistryDep, SessionDep, TracingDep, ensure_model
from be_agent.core.model_registry import ModelNotAvailable
from be_agent.core.observability import Tracing
from be_agent.db.models import Agent, Run, Thread, User
from be_agent.schemas.threads import ChatRequest, ThreadCreate, ThreadRead, ThreadUpdate, UIMessage
from be_agent.streaming.ai_sdk import AI_SDK_HEADERS, encode_ai_sdk_stream, run_message_ids, to_ui_messages
from be_agent.streaming.background import stream_in_task

router = APIRouter(prefix="/threads", tags=["threads"])


async def _get_thread_or_404(session: SessionDep, thread_id: str, user: User) -> Thread:
    thread = await session.get(Thread, thread_id)
    if thread is None or thread.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "스레드를 찾을 수 없습니다.")
    return thread


@router.get("", response_model=list[ThreadRead])
async def list_threads(session: SessionDep, user: CurrentUserDep) -> list[Thread]:
    result = await session.scalars(select(Thread).where(Thread.user_id == user.id).order_by(Thread.updated_at.desc()))
    return list(result)


@router.post("", response_model=ThreadRead, status_code=status.HTTP_201_CREATED)
async def create_thread(
    body: ThreadCreate, session: SessionDep, registry: ModelRegistryDep, user: CurrentUserDep
) -> Thread:
    ensure_model(registry, body.model)
    if body.agent_id is not None:
        agent = await session.get(Agent, body.agent_id)
        if agent is None or agent.user_id != user.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "에이전트를 찾을 수 없습니다.")
    thread = Thread(title=body.title, model=body.model, agent_id=body.agent_id, user_id=user.id)
    session.add(thread)
    await session.commit()
    return thread


@router.get("/{thread_id}", response_model=ThreadRead)
async def get_thread(thread_id: str, session: SessionDep, user: CurrentUserDep) -> Thread:
    return await _get_thread_or_404(session, thread_id, user)


@router.patch("/{thread_id}", response_model=ThreadRead)
async def update_thread(
    thread_id: str, body: ThreadUpdate, session: SessionDep, registry: ModelRegistryDep, user: CurrentUserDep
) -> Thread:
    thread = await _get_thread_or_404(session, thread_id, user)
    ensure_model(registry, body.model)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(thread, field, value)
    await session.commit()
    return thread


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: str, session: SessionDep, agent: AgentServiceDep, user: CurrentUserDep) -> None:
    thread = await _get_thread_or_404(session, thread_id, user)
    await agent.delete_thread(thread_id)
    await session.delete(thread)
    await session.commit()


@router.get("/{thread_id}/messages", response_model=list[UIMessage])
async def list_messages(
    thread_id: str, session: SessionDep, agent: AgentServiceDep, user: CurrentUserDep
) -> list[dict]:
    await _get_thread_or_404(session, thread_id, user)
    messages = to_ui_messages(await agent.get_messages(thread_id))
    # 답변마다 남긴 평가를 붙인다 (새로고침해도 누른 버튼이 보이게)
    feedback = dict(
        (await session.execute(select(Run.id, Run.feedback).where(Run.thread_id == thread_id))).tuples().all()
    )
    for message in messages:
        run_id = (message.get("metadata") or {}).get("runId")
        if run_id and (value := feedback.get(run_id)) is not None:
            message["metadata"]["feedback"] = value
    return messages


@router.post(
    "/{thread_id}/chat",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "AI SDK UI message stream (SSE)"}},
)
async def chat(
    thread_id: str,
    body: ChatRequest,
    session: SessionDep,
    agent: AgentServiceDep,
    registry: ModelRegistryDep,
    tracing: TracingDep,
    user: CurrentUserDep,
) -> StreamingResponse:
    """메시지를 보내고 에이전트 응답을 SSE 로 스트리밍한다 (Vercel AI SDK useChat 호환)."""
    thread = await _get_thread_or_404(session, thread_id, user)
    ensure_model(registry, body.model)
    agent_def = await session.get(Agent, thread.agent_id) if thread.agent_id else None
    try:
        if body.model:
            model_id, model_config = body.model, registry.resolve(body.model)
        else:
            # 예전에 쓰던 모델이 지금은 없으면(키 삭제 등) 기본 모델로 이어간다
            model_id, model_config = registry.resolve_or_default(thread.model or (agent_def and agent_def.model))
    except ModelNotAvailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    spec = (
        AgentSpec(model=model_config, system_prompt=agent_def.system_prompt, tools=tuple(agent_def.tools))
        if agent_def
        else AgentSpec(model=model_config)
    )

    if thread.title is None:
        thread.title = body.message[:50]
    thread.model = model_id
    await session.commit()

    run_id = uuid.uuid4().hex
    run = Run(id=run_id, user_id=user.id, kind="chat", thread_id=thread_id, trace_id=Tracing.trace_id_for(run_id))
    session.add(run)
    await session.commit()

    user_message_id, answer_message_id = run_message_ids(run_id)
    name = f"대화: {agent_def.name}" if agent_def else "대화"
    tags = ["chat", *([f"agent:{agent_def.name}"] if agent_def else [])]
    user_id, trace_id = user.id, run.trace_id

    async def traced() -> AsyncIterator[str]:
        # 대화 한 번 = Langfuse 기록 하나. 모델·도구 호출은 이 기록 아래에 붙는다.
        with tracing.trace(
            name, user_id=user_id, session_id=thread_id, tags=tags, input=body.message, trace_id=trace_id
        ):
            events = agent.stream(
                thread_id=thread_id,
                message=body.message,
                spec=spec,
                run_name="에이전트",
                message_id=user_message_id,
            )
            async for chunk in encode_ai_sdk_stream(events, message_id=answer_message_id, metadata={"runId": run_id}):
                yield chunk

    return StreamingResponse(stream_in_task(traced), media_type="text/event-stream", headers=AI_SDK_HEADERS)
