"""텔레그램 봇 API. 사용자가 만든 봇으로 사용자 자신에게 메시지를 보낸다.

- 봇 토큰은 @BotFather 에서 받는다.
- 채팅 ID 는 사용자가 봇에게 먼저 보낸 메시지(getUpdates)에서 찾는다. 봇은 먼저 말을 걸어 온 사람에게만 보낼 수 있다.
- 모델 답변은 마크다운이라 텔레그램 HTML 로 바꿔 보낸다. 바꾼 결과를 텔레그램이 거부하면 원문 그대로 다시 보낸다.
"""

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

from be_agent.core.errors import UserFacingError

API_BASE = "https://api.telegram.org"
MAX_LENGTH = 4096
_CHUNK = 3500  # HTML 태그가 붙어 길어질 여유를 둔다
_TIMEOUT = 15.0


class TelegramError(UserFacingError):
    """사용자에게 그대로 보여 줄 수 있는 메시지를 담는다."""


@dataclass(frozen=True)
class Bot:
    username: str
    name: str


@dataclass(frozen=True)
class Chat:
    id: str
    name: str


def _friendly(status: int, description: str) -> TelegramError:
    text = description.lower()
    if status == 401 or status == 404:
        return TelegramError("봇 토큰이 올바르지 않습니다. @BotFather 가 준 토큰을 그대로 붙여 넣으세요.")
    if "blocked" in text:
        return TelegramError("봇이 차단되어 있습니다. 텔레그램에서 봇 차단을 풀고 다시 시도하세요.")
    if "chat not found" in text:
        return TelegramError("채팅을 찾을 수 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 연결하세요.")
    if status == 409:
        return TelegramError("이 봇에 웹훅이 설정되어 있어 메시지를 읽을 수 없습니다. 웹훅을 지운 뒤 다시 시도하세요.")
    if status == 429:
        return TelegramError("텔레그램 요청이 너무 많습니다. 잠시 뒤에 다시 시도하세요.")
    return TelegramError(f"텔레그램 오류 ({status}): {description}")


async def _call(token: str, method: str, **params: Any) -> Any:
    try:
        async with httpx.AsyncClient(base_url=API_BASE, timeout=_TIMEOUT) as client:
            response = await client.post(f"/bot{token}/{method}", json=params)
    except httpx.HTTPError as exc:
        raise TelegramError("텔레그램에 연결하지 못했습니다. 네트워크를 확인하세요.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("ok"):
        raise _friendly(response.status_code, str(body.get("description", response.text[:200])))
    return body["result"]


async def get_bot(token: str) -> Bot:
    """토큰이 맞는지 확인하고 봇 이름을 받는다."""
    me = await _call(token, "getMe")
    return Bot(username=me.get("username", ""), name=me.get("first_name", ""))


async def find_chat(token: str) -> Chat | None:
    """봇에게 최근 말을 건 개인 채팅. 없으면 None."""
    updates = await _call(token, "getUpdates", limit=100, allowed_updates=["message"])
    for update in reversed(updates):
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
            return Chat(id=str(chat["id"]), name=name)
    return None


# ---------- 마크다운 → 텔레그램 HTML ----------

_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.M)


def to_html(markdown: str) -> str:
    """텔레그램이 알아듣는 태그(b, code, pre, a)만 쓴다. 나머지는 글자 그대로 둔다."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = _CODE_BLOCK.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), markdown)
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'), text
    )
    text = html.escape(text, quote=False)
    text = _HEADING.sub(r"<b>\1</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _BULLET.sub(r"\1• ", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def split_text(text: str, limit: int = _CHUNK) -> list[str]:
    """문단 → 줄 → 글자 순으로 끊어 limit 이하 조각으로 나눈다."""
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > limit:
            cut = paragraph.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip("\n")
        current = paragraph
    if current:
        chunks.append(current)
    return chunks or [""]


async def send_message(token: str, chat_id: str, text: str) -> int:
    """긴 글은 나눠 보낸다. 보낸 메시지 수를 돌려준다."""
    if not text.strip():
        raise TelegramError("보낼 내용이 비어 있습니다.")
    chunks = split_text(text)
    for chunk in chunks:
        try:
            await _call(
                token,
                "sendMessage",
                chat_id=chat_id,
                text=to_html(chunk),
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
            )
        except TelegramError as exc:
            if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                raise
            # 서식을 못 알아들으면 원문 그대로
            await _call(token, "sendMessage", chat_id=chat_id, text=chunk, link_preview_options={"is_disabled": True})
    return len(chunks)
