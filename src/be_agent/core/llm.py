"""모델 팩토리. 모델 생성은 반드시 이곳을 거쳐서, 제공사별 설정을 한곳에서 관리한다."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from be_agent.core.fake_model import FakeToolChatModel


def create_chat_model(name: str) -> BaseChatModel:
    provider, _, model = name.partition(":")
    if not model:
        raise ValueError(f"모델 이름은 '<provider>:<model>' 형식이어야 합니다: {name!r}")
    if provider == "fake":
        return FakeToolChatModel()
    return init_chat_model(name)
