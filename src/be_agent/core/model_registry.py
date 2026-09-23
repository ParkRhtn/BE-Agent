"""사용자별로 쓸 수 있는 모델 목록과, 모델 ID → 실제 접속 설정 변환.

모델 ID
- 설정 화면에서 등록한 제공사: "<제공사ID>:<모델>"  (예: "3f2a…:claude-sonnet-5")
- .env 대체 모델: "<provider>:<model>"             (예: "anthropic:claude-sonnet-5", 로컬 전용 "fake:echo")
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from be_agent.core.config import Settings
from be_agent.core.crypto import SecretBox
from be_agent.core.llm import ModelConfig
from be_agent.db.models import ModelProvider, User
from be_agent.schemas.models import ModelOption

_ENV_PROVIDER_LABEL = {"anthropic": "Anthropic (.env)", "openai": "OpenAI (.env)", "fake": "개발용"}


class ModelNotAvailable(ValueError):
    pass


@dataclass
class ModelRegistry:
    options: list[ModelOption]
    configs: dict[str, ModelConfig]
    default_id: str | None

    def resolve(self, model_id: str | None) -> ModelConfig:
        """없는 모델이면 ModelNotAvailable. None 이면 기본 모델."""
        target = model_id or self.default_id
        if target is None:
            raise ModelNotAvailable("사용할 수 있는 모델이 없습니다. 설정에서 모델 제공사를 추가하세요.")
        if target not in self.configs:
            raise ModelNotAvailable(f"사용할 수 없는 모델입니다: {target}")
        return self.configs[target]

    def resolve_or_default(self, model_id: str | None) -> tuple[str, ModelConfig]:
        """저장돼 있던 모델이 더 이상 없으면 기본 모델로 대신한다 (예전 스레드·에이전트용)."""
        chosen = model_id if model_id in self.configs else self.default_id
        return chosen or "", self.resolve(chosen)


def _env_configs(settings: Settings) -> dict[str, tuple[ModelConfig, str]]:
    keys = {"anthropic": settings.anthropic_api_key, "openai": settings.openai_api_key}
    result: dict[str, tuple[ModelConfig, str]] = {}
    for model_id in settings.allowed_models:
        provider, _, model = model_id.partition(":")
        if provider == "fake":
            if settings.environment == "local":
                result[model_id] = (ModelConfig(provider="fake", model=model), model_id)
        elif provider in keys and keys[provider]:
            result[model_id] = (ModelConfig(provider=provider, model=model, api_key=keys[provider]), model)  # type: ignore[arg-type]
    return result


async def load_registry(session: AsyncSession, user: User, settings: Settings) -> ModelRegistry:
    box = SecretBox(settings.secret_box_key)
    options: list[ModelOption] = []
    configs: dict[str, ModelConfig] = {}

    providers = await session.scalars(
        select(ModelProvider).where(ModelProvider.user_id == user.id).order_by(ModelProvider.created_at)
    )
    for provider in providers:
        api_key = box.decrypt(provider.api_key_encrypted) if provider.api_key_encrypted else None
        labels = {m["id"]: m["label"] for m in provider.available_models}
        for model in provider.enabled_models:
            model_id = f"{provider.id}:{model}"
            configs[model_id] = ModelConfig(
                provider=provider.kind,  # type: ignore[arg-type]
                model=model,
                api_key=api_key,
                base_url=provider.base_url,
            )
            options.append(ModelOption(id=model_id, label=labels.get(model, model), provider=provider.name))
    user_model_count = len(options)

    for model_id, (config, label) in _env_configs(settings).items():
        configs[model_id] = config
        options.append(ModelOption(id=model_id, label=label, provider=_ENV_PROVIDER_LABEL[config.provider]))

    if user.default_model in configs:
        default = user.default_model
    elif user_model_count:
        default = options[0].id  # 직접 등록한 모델이 .env 대체 모델보다 우선
    elif settings.default_model in configs:
        default = settings.default_model
    else:
        default = options[0].id if options else None
    return ModelRegistry(options=options, configs=configs, default_id=default)
