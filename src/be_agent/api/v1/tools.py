from fastapi import APIRouter
from langchain_core.utils.function_calling import convert_to_openai_tool

from be_agent.api.deps import AgentServiceDep
from be_agent.schemas.agents import ToolRead
from be_agent.tools.catalog import display_for, example_args

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=list[ToolRead])
async def list_tools(service: AgentServiceDep) -> list[ToolRead]:
    tools = []
    for tool in service.tools:
        display = display_for(tool.name, tool.description)
        schema = convert_to_openai_tool(tool)["function"].get("parameters", {})
        tools.append(
            ToolRead(
                name=tool.name,
                label=display.label,
                description=display.description,
                example_args=example_args(tool.name, schema),
            )
        )
    return tools
