from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from be_agent.api.deps import CurrentUserDep, SessionDep
from be_agent.core.security import new_api_key
from be_agent.db.models import ApiKey, Workflow
from be_agent.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyRead

# 키 관리는 로그인(화면)에서만. API 키로는 새 키를 만들거나 지울 수 없다.
router = APIRouter(prefix="/api-keys", tags=["api-keys"])

MAX_KEYS = 20


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(session: SessionDep, user: CurrentUserDep) -> list[ApiKey]:
    result = await session.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()))
    return list(result)


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(body: ApiKeyCreate, session: SessionDep, user: CurrentUserDep) -> ApiKeyCreated:
    """키 원문은 이 응답에서만 한 번 보여 준다."""
    count = await session.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user.id))
    if (count or 0) >= MAX_KEYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"API 키는 {MAX_KEYS}개까지 만들 수 있습니다.")
    workflow_ids = None
    if body.workflow_ids is not None:
        workflow_ids = sorted(set(body.workflow_ids))
        owned = await session.scalars(
            select(Workflow.id).where(Workflow.user_id == user.id, Workflow.id.in_(workflow_ids))
        )
        if set(owned) != set(workflow_ids):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "없는 워크플로우가 포함되어 있습니다.")
    key, key_hash, prefix = new_api_key()
    api_key = ApiKey(
        user_id=user.id, name=body.name.strip(), key_hash=key_hash, prefix=prefix, workflow_ids=workflow_ids
    )
    session.add(api_key)
    await session.commit()
    return ApiKeyCreated.model_validate({**ApiKeyRead.model_validate(api_key).model_dump(), "key": key})


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(key_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    """지우면 그 키로 오는 요청은 바로 거부된다."""
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API 키를 찾을 수 없습니다.")
    await session.delete(api_key)
    await session.commit()
