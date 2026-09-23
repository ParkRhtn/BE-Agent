from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update

from be_agent.agent.service import AgentService
from be_agent.api.deps import AgentServiceDep, CurrentUserDep, SessionDep, SettingsDep
from be_agent.api.v1.threads import validate_model
from be_agent.db.models import Agent, Thread, User
from be_agent.schemas.agents import AgentCreate, AgentRead, AgentUpdate

router = APIRouter(prefix="/agents", tags=["agents"])


def _validate_tools(service: AgentService, tools: list[str] | None) -> None:
    unknown = set(tools or []) - {t.name for t in service.tools}
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"알 수 없는 도구입니다: {sorted(unknown)}")


async def _get_agent_or_404(session: SessionDep, agent_id: str, user: User) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "에이전트를 찾을 수 없습니다.")
    return agent


@router.get("", response_model=list[AgentRead])
async def list_agents(session: SessionDep, user: CurrentUserDep) -> list[Agent]:
    result = await session.scalars(select(Agent).where(Agent.user_id == user.id).order_by(Agent.updated_at.desc()))
    return list(result)


@router.post("", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate, session: SessionDep, settings: SettingsDep, service: AgentServiceDep, user: CurrentUserDep
) -> Agent:
    validate_model(settings, body.model)
    _validate_tools(service, body.tools)
    agent = Agent(**body.model_dump(), user_id=user.id)
    session.add(agent)
    await session.commit()
    return agent


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: str, session: SessionDep, user: CurrentUserDep) -> Agent:
    return await _get_agent_or_404(session, agent_id, user)


@router.patch("/{agent_id}", response_model=AgentRead)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    session: SessionDep,
    settings: SettingsDep,
    service: AgentServiceDep,
    user: CurrentUserDep,
) -> Agent:
    agent = await _get_agent_or_404(session, agent_id, user)
    validate_model(settings, body.model)
    _validate_tools(service, body.tools)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(agent, field, value)
    await session.commit()
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    agent = await _get_agent_or_404(session, agent_id, user)
    # SQLite 는 기본적으로 FK 를 강제하지 않으므로 직접 연결을 끊는다. 스레드는 기본 에이전트로 계속 쓸 수 있다.
    await session.execute(update(Thread).where(Thread.agent_id == agent_id).values(agent_id=None))
    await session.delete(agent)
    await session.commit()
