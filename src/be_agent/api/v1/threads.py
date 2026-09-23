from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from be_agent.agent.service import AgentSpec
from be_agent.api.deps import AgentServiceDep, CurrentUserDep, SessionDep, SettingsDep
from be_agent.core.config import Settings
from be_agent.db.models import Agent, Thread, User
from be_agent.schemas.threads import ChatRequest, ThreadCreate, ThreadRead, ThreadUpdate, UIMessage
from be_agent.streaming.ai_sdk import AI_SDK_HEADERS, encode_ai_sdk_stream, to_ui_messages

router = APIRouter(prefix="/threads", tags=["threads"])


def validate_model(settings: Settings, model: str | None) -> None:
    if model is not None and model not in settings.allowed_models:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"허용되지 않은 모델입니다: {model}")


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
async def create_thread(body: ThreadCreate, session: SessionDep, settings: SettingsDep, user: CurrentUserDep) -> Thread:
    validate_model(settings, body.model)
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
    thread_id: str, body: ThreadUpdate, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> Thread:
    thread = await _get_thread_or_404(session, thread_id, user)
    validate_model(settings, body.model)
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
    return to_ui_messages(await agent.get_messages(thread_id))


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
    settings: SettingsDep,
    user: CurrentUserDep,
) -> StreamingResponse:
    """메시지를 보내고 에이전트 응답을 SSE 로 스트리밍한다 (Vercel AI SDK useChat 호환)."""
    thread = await _get_thread_or_404(session, thread_id, user)
    validate_model(settings, body.model)
    agent_def = await session.get(Agent, thread.agent_id) if thread.agent_id else None
    model_name = body.model or thread.model or (agent_def and agent_def.model) or settings.default_model
    spec = (
        AgentSpec(model=model_name, system_prompt=agent_def.system_prompt, tools=tuple(agent_def.tools))
        if agent_def
        else AgentSpec(model=model_name)
    )

    if thread.title is None:
        thread.title = body.message[:50]
    thread.model = model_name
    await session.commit()

    events = agent.stream(thread_id=thread_id, message=body.message, spec=spec)
    return StreamingResponse(
        encode_ai_sdk_stream(events),
        media_type="text/event-stream",
        headers=AI_SDK_HEADERS,
    )
