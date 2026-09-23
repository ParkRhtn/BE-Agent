from fastapi import APIRouter, Depends

from be_agent.api.deps import get_current_user
from be_agent.api.v1 import agents, auth, models, threads, tools, workflows

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(threads.router)
api_router.include_router(agents.router)
api_router.include_router(workflows.router)
# 사용자별 데이터는 없지만 로그인한 사용자에게만 공개한다.
api_router.include_router(tools.router, dependencies=[Depends(get_current_user)])
api_router.include_router(models.router, dependencies=[Depends(get_current_user)])
