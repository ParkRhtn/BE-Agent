"""외부 서비스 연결. 지금은 텔레그램 봇 하나."""

from fastapi import APIRouter, HTTPException, status

from be_agent.api.deps import CurrentUserDep, SessionDep, SettingsDep
from be_agent.core import telegram
from be_agent.core.crypto import SecretBox, key_hint
from be_agent.db.models import TelegramLink
from be_agent.schemas.integrations import TelegramConnect, TelegramStatus

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _status(link: TelegramLink | None) -> TelegramStatus:
    if link is None:
        return TelegramStatus(connected=False)
    return TelegramStatus(
        connected=True, bot_username=link.bot_username, chat_name=link.chat_name, token_hint=link.token_hint
    )


@router.get("/telegram", response_model=TelegramStatus)
async def get_telegram(session: SessionDep, user: CurrentUserDep) -> TelegramStatus:
    return _status(await session.get(TelegramLink, user.id))


@router.put("/telegram", response_model=TelegramStatus)
async def connect_telegram(
    body: TelegramConnect, session: SessionDep, settings: SettingsDep, user: CurrentUserDep
) -> TelegramStatus:
    """토큰을 확인하고, 봇에게 말을 건 내 채팅을 찾아 연결한다. 연결되면 확인 메시지를 보낸다."""
    token = body.bot_token.strip()
    try:
        bot = await telegram.get_bot(token)
        chat = await telegram.find_chat(token)
        if chat is None:
            raise telegram.TelegramError(
                f"봇(@{bot.username})에게 온 메시지가 없습니다. 텔레그램에서 @{bot.username} 을 열어 "
                "시작(/start)을 누르거나 아무 메시지나 보낸 뒤 다시 연결하세요."
            )
        await telegram.send_message(
            token, chat.id, "✅ Agent 와 연결되었습니다. 워크플로우 결과를 여기로 보내 드릴게요."
        )
    except telegram.TelegramError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    link = await session.get(TelegramLink, user.id) or TelegramLink(user_id=user.id)
    link.bot_token_encrypted = SecretBox(settings.secret_box_key).encrypt(token)
    link.token_hint = key_hint(token)
    link.bot_username = bot.username
    link.chat_id = chat.id
    link.chat_name = chat.name
    session.add(link)
    await session.commit()
    return _status(link)


@router.post("/telegram/test", status_code=status.HTTP_204_NO_CONTENT)
async def test_telegram(session: SessionDep, settings: SettingsDep, user: CurrentUserDep) -> None:
    link = await session.get(TelegramLink, user.id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "텔레그램이 연결되어 있지 않습니다.")
    try:
        token = SecretBox(settings.secret_box_key).decrypt(link.bot_token_encrypted)
        await telegram.send_message(token, link.chat_id, "🔔 테스트 메시지입니다. 잘 도착했나요?")
    except (telegram.TelegramError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.delete("/telegram", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_telegram(session: SessionDep, user: CurrentUserDep) -> None:
    if link := await session.get(TelegramLink, user.id):
        await session.delete(link)
        await session.commit()
