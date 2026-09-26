"""모델 팩토리. 모델 생성은 반드시 이곳을 거쳐서, 제공사별 설정을 한곳에서 관리한다."""

from dataclasses import dataclass
from typing import Any, Literal

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from be_agent.core.fake_model import FakeToolChatModel

ProviderKind = Literal["anthropic", "openai", "openai_compatible", "fake"]
# 누구 키로 부르나. platform = 서버 키(.env, 운영자가 비용 부담 → 크레딧 차감), user = 사용자가 등록한 키, free = 개발용
Billing = Literal["platform", "user", "free"]


@dataclass(frozen=True)
class ModelConfig:
    """모델 하나를 만드는 데 필요한 전부. 에이전트 그래프 캐시 키로도 쓰인다."""

    provider: ProviderKind
    model: str
    api_key: str | None = None
    base_url: str | None = None
    billing: Billing = "platform"  # 모르면 차감 쪽으로 (운영자가 비용을 떠안지 않게)


def create_chat_model(config: ModelConfig, **kwargs: Any) -> BaseChatModel:
    # 모델 호출 콜백(UsageCallback)이 호출마다 과금 주체를 알 수 있게 모델에 붙여 둔다
    kwargs["metadata"] = {**kwargs.get("metadata", {}), "billing": config.billing}
    if config.provider == "fake":
        return FakeToolChatModel(metadata=kwargs["metadata"])
    # OpenAI 호환 서버(Ollama 등)는 openai 클라이언트에 주소만 바꿔 쓴다
    provider = "openai" if config.provider == "openai_compatible" else config.provider
    if config.api_key:
        kwargs["api_key"] = config.api_key
    elif config.provider == "openai_compatible":
        kwargs["api_key"] = "not-needed"  # 키 없는 로컬 서버도 클라이언트는 값을 요구한다
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return init_chat_model(config.model, model_provider=provider, **kwargs)
