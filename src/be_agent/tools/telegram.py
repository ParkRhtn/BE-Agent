"""텔레그램 보내기 도구. 설정에서 연결한 봇으로, 지금 실행 중인 사용자 자신에게 보낸다."""

from langchain_core.tools import BaseTool, tool
from sqlalchemy.ext.asyncio import async_sessionmaker

from be_agent.core import telegram
from be_agent.core.context import current_user_id
from be_agent.core.crypto import SecretBox
from be_agent.core.errors import UserFacingError
from be_agent.db.models import TelegramLink


class TelegramNotConnected(UserFacingError):
    pass


def make_telegram_tool(sessionmaker: async_sessionmaker, box: SecretBox) -> BaseTool:
    @tool("send_telegram")
    async def send_telegram(text: str) -> str:
        """Send a message to the user's own Telegram chat (via the bot they connected in settings).
        Markdown (bold, lists, links, code) is supported. Long text is split automatically."""
        user_id = current_user_id.get()
        async with sessionmaker() as db:
            link = await db.get(TelegramLink, user_id) if user_id else None
        if link is None:
            raise TelegramNotConnected("텔레그램이 연결되어 있지 않습니다. 설정 → 텔레그램에서 봇을 연결하세요.")
        count = await telegram.send_message(box.decrypt(link.bot_token_encrypted), link.chat_id, text)
        return f"텔레그램으로 보냈습니다 ({len(text)}자, 메시지 {count}개)."

    return send_telegram
