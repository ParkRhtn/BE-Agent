import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from be_agent.api.deps import CurrentUserDep, SessionDep, SettingsDep
from be_agent.core.config import Settings
from be_agent.core.crypto import SecretBox, key_hint
from be_agent.core.llm import ModelConfig
from be_agent.core.providers import ProviderCheckError, list_models, test_model
from be_agent.db.models import ModelProvider, User
from be_agent.schemas.models import (
    ModelTestRequest,
    ModelTestResult,
    ProviderCreate,
    ProviderRead,
    ProviderUpdate,
)

router = APIRouter(prefix="/providers", tags=["providers"])

_DEFAULT_NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "openai_compatible": "OpenAI 호환"}


def _clean_base_url(kind: str, base_url: str | None) -> str | None:
    if kind != "openai_compatible":
        return None  # 공식 제공사는 주소를 바꾸지 않는다
    url = (base_url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "서버 주소를 http:// 또는 https:// 로 시작하게 입력하세요.")
    return url


async def _verify(kind: str, api_key: str | None, base_url: str | None) -> list[dict]:
    if kind != "openai_compatible" and not api_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "API 키를 입력하세요.")
    try:
        models = await list_models(kind, api_key=api_key, base_url=base_url)
    except ProviderCheckError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not models:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "연결은 됐지만 쓸 수 있는 모델이 없습니다.")
    return [{"id": m.id, "label": m.label} for m in models]


def _api_key(settings: Settings, provider: ModelProvider) -> str | None:
    if not provider.api_key_encrypted:
        return None
    return SecretBox(settings.secret_box_key).decrypt(provider.api_key_encrypted)


async def _get_or_404(session: SessionDep, provider_id: str, user: User) -> ModelProvider:
    provider = await session.get(ModelProvider, provider_id)
    if provider is None or provider.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "제공사를 찾을 수 없습니다.")
    return provider


@router.get("", response_model=list[ProviderRead])
async def list_providers(session: SessionDep, user: CurrentUserDep) -> list[ModelProvider]:
    result = await session.scalars(
        select(ModelProvider).where(ModelProvider.user_id == user.id).order_by(ModelProvider.created_at)
    )
    return list(result)


@router.post("", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: ProviderCreate, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> ModelProvider:
    """실제 API 로 키를 확인한 뒤에만 저장한다."""
    api_key = (body.api_key or "").strip() or None
    base_url = _clean_base_url(body.kind, body.base_url)
    available = await _verify(body.kind, api_key, base_url)

    provider = ModelProvider(
        user_id=user.id,
        kind=body.kind,
        name=(body.name or "").strip() or _DEFAULT_NAMES[body.kind],
        base_url=base_url,
        api_key_encrypted=SecretBox(settings.secret_box_key).encrypt(api_key) if api_key else None,
        api_key_hint=key_hint(api_key) if api_key else None,
        available_models=available,
        enabled_models=[],
        verified_at=datetime.now(UTC),
    )
    session.add(provider)
    await session.commit()
    return provider


@router.patch("/{provider_id}", response_model=ProviderRead)
async def update_provider(
    provider_id: str, body: ProviderUpdate, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> ModelProvider:
    provider = await _get_or_404(session, provider_id, user)
    changes = body.model_dump(exclude_unset=True)

    if "api_key" in changes or "base_url" in changes:
        # 접속 정보가 바뀌면 다시 확인하고, 통과해야 바꾼다
        api_key = (changes.get("api_key") or "").strip() or _api_key(settings, provider)
        base_url = _clean_base_url(provider.kind, changes.get("base_url", provider.base_url))
        provider.available_models = await _verify(provider.kind, api_key, base_url)
        provider.base_url = base_url
        if changes.get("api_key"):
            provider.api_key_encrypted = SecretBox(settings.secret_box_key).encrypt(api_key or "")
            provider.api_key_hint = key_hint(api_key or "")
        provider.verified_at = datetime.now(UTC)
        available_ids = {m["id"] for m in provider.available_models}
        provider.enabled_models = [m for m in provider.enabled_models if m in available_ids]

    if "name" in changes and changes["name"]:
        provider.name = changes["name"].strip()
    if changes.get("enabled_models") is not None:
        available_ids = {m["id"] for m in provider.available_models}
        unknown = [m for m in changes["enabled_models"] if m not in available_ids]
        if unknown:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"이 제공사에 없는 모델입니다: {unknown}")
        provider.enabled_models = list(dict.fromkeys(changes["enabled_models"]))

    await session.commit()
    return provider


@router.post("/{provider_id}/verify", response_model=ProviderRead)
async def verify_provider(
    provider_id: str, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> ModelProvider:
    """저장된 키로 다시 확인하고 모델 목록을 새로 받는다."""
    provider = await _get_or_404(session, provider_id, user)
    provider.available_models = await _verify(provider.kind, _api_key(settings, provider), provider.base_url)
    available_ids = {m["id"] for m in provider.available_models}
    provider.enabled_models = [m for m in provider.enabled_models if m in available_ids]
    provider.verified_at = datetime.now(UTC)
    await session.commit()
    return provider


@router.post("/{provider_id}/test", response_model=ModelTestResult)
async def test_provider_model(
    provider_id: str, body: ModelTestRequest, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> ModelTestResult:
    """모델에 짧은 요청을 실제로 보내본다. 크레딧 부족·모델 권한 문제도 여기서 드러난다."""
    provider = await _get_or_404(session, provider_id, user)
    if body.model not in {m["id"] for m in provider.available_models}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"이 제공사에 없는 모델입니다: {body.model}")
    config = ModelConfig(
        provider=provider.kind,  # type: ignore[arg-type]
        model=body.model,
        api_key=_api_key(settings, provider),
        base_url=provider.base_url,
    )
    started = time.perf_counter()
    try:
        reply = await test_model(config)
    except ProviderCheckError as exc:
        return ModelTestResult(ok=False, error=str(exc))
    return ModelTestResult(ok=True, reply=reply, latency_ms=round((time.perf_counter() - started) * 1000))


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: str, session: SessionDep, user: CurrentUserDep) -> None:
    provider = await _get_or_404(session, provider_id, user)
    await session.delete(provider)
    await session.commit()
