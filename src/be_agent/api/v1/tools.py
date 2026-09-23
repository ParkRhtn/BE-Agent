from fastapi import APIRouter

from be_agent.api.deps import AgentServiceDep
from be_agent.schemas.agents import ToolRead

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolRead])
async def list_tools(service: AgentServiceDep) -> list[ToolRead]:
    return [ToolRead(name=t.name, description=t.description) for t in service.tools]
