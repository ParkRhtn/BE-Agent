from fastapi import APIRouter

from be_agent.api.deps import CurrentUserDep, ModelRegistryDep, SessionDep, ensure_model
from be_agent.schemas.models import DefaultModelUpdate, ModelsRead

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelsRead)
async def list_models(registry: ModelRegistryDep) -> ModelsRead:
    """이 사용자가 쓸 수 있는 모델 (설정에서 켠 모델 + .env 대체 모델)."""
    return ModelsRead(default=registry.default_id, options=registry.options)


@router.put("/default", response_model=ModelsRead)
async def set_default_model(
    body: DefaultModelUpdate, session: SessionDep, registry: ModelRegistryDep, user: CurrentUserDep
) -> ModelsRead:
    ensure_model(registry, body.model)
    user.default_model = body.model
    await session.commit()
    registry.default_id = body.model
    return ModelsRead(default=registry.default_id, options=registry.options)
