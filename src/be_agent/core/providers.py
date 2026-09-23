"""모델 제공사 연결 확인. 실제 API 를 호출해 키가 유효한지 보고, 쓸 수 있는 모델 목록을 받아온다."""

import re
from dataclasses import dataclass

import anthropic
import openai

from be_agent.core.llm import ModelConfig, create_chat_model

_TIMEOUT = 15.0

# OpenAI /models 에는 임베딩·음성·이미지 모델도 섞여 있다. 채팅에 못 쓰는 것은 뺀다.
_OPENAI_NON_CHAT = re.compile(
    r"embedding|tts|whisper|dall-e|davinci|babbage|moderation|image|audio|transcribe|realtime|search|sora"
)


_NO_CREDIT = {
    "openai": "API 크레딧이 없습니다. platform.openai.com → Settings → Billing 에서 결제 수단을 등록하고 충전하세요. "
    "(ChatGPT 구독에는 API 크레딧이 포함되지 않습니다)",
    "anthropic": "API 크레딧이 부족합니다. console.anthropic.com → Settings → Billing 에서 충전하세요.",
}


class ProviderCheckError(Exception):
    """사용자에게 그대로 보여줄 수 있는 메시지를 담는다."""


@dataclass(frozen=True)
class ProviderModel:
    id: str
    label: str


def _friendly(exc: Exception) -> ProviderCheckError:
    match exc:
        case anthropic.AuthenticationError() | openai.AuthenticationError():
            return ProviderCheckError("API 키가 올바르지 않습니다.")
        case anthropic.PermissionDeniedError() | openai.PermissionDeniedError():
            return ProviderCheckError("이 키에는 권한이 없습니다. 키의 권한(스코프)을 확인하세요.")
        case anthropic.APITimeoutError() | openai.APITimeoutError():
            return ProviderCheckError("응답 시간이 초과되었습니다. 잠시 후 다시 시도하세요.")
        case anthropic.APIConnectionError() | openai.APIConnectionError():
            return ProviderCheckError("서버에 연결할 수 없습니다. 주소와 네트워크를 확인하세요.")
        # OpenAI 는 크레딧 부족도 429 로 보낸다. code 로 요청 과다와 구분한다.
        case openai.RateLimitError() if exc.code == "insufficient_quota":
            return ProviderCheckError(_NO_CREDIT["openai"])
        case anthropic.RateLimitError() | openai.RateLimitError():
            return ProviderCheckError("요청이 너무 많아 제한되었습니다. 잠시 후 다시 시도하세요.")
        # Anthropic 은 크레딧 부족을 400 으로 보낸다
        case anthropic.BadRequestError() if "credit balance" in str(exc.message).lower():
            return ProviderCheckError(_NO_CREDIT["anthropic"])
        case anthropic.APIStatusError() | openai.APIStatusError():
            return ProviderCheckError(f"제공사 오류 ({exc.status_code}): {exc.message}")
    return ProviderCheckError(f"{type(exc).__name__}: {exc}")


async def list_models(kind: str, *, api_key: str | None, base_url: str | None) -> list[ProviderModel]:
    """키로 모델 목록을 조회한다. 조회가 되면 키가 유효하다는 뜻이다 (토큰 비용 없음)."""
    try:
        if kind == "anthropic":
            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=_TIMEOUT, max_retries=0)
            return [ProviderModel(m.id, m.display_name) async for m in client.models.list()]
        if kind in ("openai", "openai_compatible"):
            oa = openai.AsyncOpenAI(
                api_key=api_key or "not-needed", base_url=base_url or None, timeout=_TIMEOUT, max_retries=0
            )
            ids = sorted([m.id async for m in oa.models.list()])
            if kind == "openai":
                ids = [i for i in ids if not _OPENAI_NON_CHAT.search(i)]
            return [ProviderModel(i, i) for i in ids]
    except Exception as exc:
        raise _friendly(exc) from exc
    raise ProviderCheckError(f"알 수 없는 제공사입니다: {kind}")


async def test_model(config: ModelConfig) -> str:
    """아주 짧은 요청을 실제로 보내 답이 오는지 확인한다 (토큰 몇 개 비용)."""
    try:
        model = create_chat_model(config, max_tokens=64, timeout=30, max_retries=0)
        reply = await model.ainvoke("Reply with just: OK")
    except Exception as exc:
        raise _friendly(exc) from exc
    return reply.text.strip()[:200]
