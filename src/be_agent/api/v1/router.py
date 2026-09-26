from fastapi import APIRouter, Depends

from be_agent.api.deps import get_current_user
from be_agent.api.v1 import (
    agents,
    api_keys,
    auth,
    credits,
    ext,
    integrations,
    models,
    providers,
    public,
    runs,
    threads,
    tools,
    workflows,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(threads.router)
api_router.include_router(agents.router)
api_router.include_router(workflows.router)
api_router.include_router(providers.router)
api_router.include_router(runs.router)
api_router.include_router(credits.router)
api_router.include_router(api_keys.router)
# 외부 서비스용 (API 키 인증)
api_router.include_router(ext.router)
# 로그인 없이 (다른 사이트에 붙인 공개 링크)
api_router.include_router(public.router)
api_router.include_router(integrations.router)
# 사용자별 데이터는 없지만 로그인한 사용자에게만 공개한다.
api_router.include_router(tools.router, dependencies=[Depends(get_current_user)])
api_router.include_router(models.router)
