from fastapi import APIRouter

from be_agent.api.deps import SettingsDep
from be_agent.schemas.threads import ModelsRead

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelsRead)
async def list_models(settings: SettingsDep) -> ModelsRead:
    return ModelsRead(default=settings.default_model, allowed=settings.allowed_models)
