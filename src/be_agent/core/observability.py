import logging

from langchain_core.callbacks import BaseCallbackHandler

from be_agent.core.config import Settings

logger = logging.getLogger(__name__)


def create_callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    """Langfuse 키가 설정되어 있으면 에이전트 실행을 Langfuse로 추적한다."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        environment=settings.environment,
    )
    logger.info("Langfuse tracing enabled")
    return [CallbackHandler(public_key=settings.langfuse_public_key)]
