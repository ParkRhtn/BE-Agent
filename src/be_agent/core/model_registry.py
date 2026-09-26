"""사용자별로 쓸 수 있는 모델 목록과, 모델 ID → 실제 접속 설정 변환.

모델 ID
- 설정 화면에서 등록한 제공사: "<제공사ID>:<모델>"  (예: "3f2a…:claude-sonnet-5")
- .env 대체 모델: "<provider>:<model>"             (예: "anthropic:claude-sonnet-5", 로컬 전용 "fake:echo")
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from be_agent.core.config import Settings
from be_agent.core.credits import balance_milli
from be_agent.core.crypto import SecretBox
from be_agent.core.llm import ModelConfig
from be_agent.core.pricing import find_price
from be_agent.db.models import ModelProvider, User
from be_agent.schemas.models import ModelOption

_ENV_PROVIDER_LABEL = {"anthropic": "Anthropic (.env)", "openai": "OpenAI (.env)", "fake": "개발용"}


class ModelNotAvailable(ValueError):
    pass


class CreditsExhausted(ModelNotAvailable):
    """서버 키 모델인데 크레딧이 없다. 사용자 키 모델은 계속 쓸 수 있다."""


@dataclass
class ModelRegistry:
    options: list[ModelOption]
    configs: dict[str, ModelConfig]
    default_id: str | None
    blocked: dict[str, str] = field(default_factory=dict)  # 모델 ID → 막힌 이유 (크레딧 부족 등)

    def blocked_reason(self, model_id: str | None) -> str | None:
        return self.blocked.get(model_id or self.default_id or "")

    def resolve(self, model_id: str | None) -> ModelConfig:
        """없는 모델이면 ModelNotAvailable. None 이면 기본 모델."""
        target = model_id or self.default_id
        if target is None:
            raise ModelNotAvailable("사용할 수 있는 모델이 없습니다. 설정에서 모델 제공사를 추가하세요.")
        if target not in self.configs:
            raise ModelNotAvailable(f"사용할 수 없는 모델입니다: {target}")
        if reason := self.blocked.get(target):
            raise CreditsExhausted(reason)
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
                result[model_id] = (ModelConfig(provider="fake", model=model, billing="free"), model_id)
        elif provider in keys and keys[provider]:
            config = ModelConfig(provider=provider, model=model, api_key=keys[provider], billing="platform")  # type: ignore[arg-type]
            result[model_id] = (config, model)
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
                billing="user",
            )
            options.append(
                ModelOption(id=model_id, label=labels.get(model, model), provider=provider.name, billing="user")
            )
    user_model_count = len(options)

    blocked: dict[str, str] = {}
    env_configs = _env_configs(settings)
    if settings.credits_enforced and any(c.billing == "platform" for c, _ in env_configs.values()):
        balance = await balance_milli(session, user.id)
        for model_id, (config, _) in env_configs.items():
            if config.billing != "platform":
                continue
            if find_price(config.model) is None:
                # 원가를 모르면 차감할 수 없으므로 운영자가 가격표를 채우기 전까지 막는다
                blocked[model_id] = f"가격표에 없는 모델이라 크레딧으로 쓸 수 없습니다: {config.model}"
            elif balance <= 0:
                blocked[model_id] = "크레딧이 부족합니다. 충전하거나, 설정에서 직접 등록한 API 키의 모델을 쓰세요."

    for model_id, (config, label) in env_configs.items():
        configs[model_id] = config
        options.append(
            ModelOption(
                id=model_id,
                label=label,
                provider=_ENV_PROVIDER_LABEL[config.provider],
                billing=config.billing,
                blocked_reason=blocked.get(model_id),
            )
        )

    if user.default_model in configs:
        default = user.default_model
    elif user_model_count:
        default = options[0].id  # 직접 등록한 모델이 .env 대체 모델보다 우선
    elif settings.default_model in configs:
        default = settings.default_model
    else:
        default = options[0].id if options else None
    return ModelRegistry(options=options, configs=configs, default_id=default, blocked=blocked)
