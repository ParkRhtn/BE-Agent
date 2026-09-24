from pydantic import BaseModel, Field


class TelegramConnect(BaseModel):
    bot_token: str = Field(min_length=10, max_length=200)


class TelegramStatus(BaseModel):
    connected: bool
    bot_username: str | None = None
    chat_name: str | None = None
    token_hint: str | None = None
