from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.anon_chat import ChatEvent, ChatState, Direction, EventType


class ChatStatus(BaseModel):
    state: ChatState
    last_seq: int = Field(..., description="Sequence number of the last recorded message")
    limited_until: datetime | None = Field(
        None, description="Until when the anonymous bot limits the account (state=limited)"
    )


class BotStatusOut(BaseModel):
    username: str
    active: bool = Field(..., description="Бот ведёт поиск или диалог прямо сейчас")
    available: bool
    until: datetime | None = Field(None, description="Когда бот снова станет доступен")
    reason: str | None = Field(None, description="limit | blocked | no_reply | send_error")


class AccountOut(BaseModel):
    id: str
    authorized: bool
    username: str | None
    bot_username: str
    mode: Literal["normal", "adult"] = "normal"
    state: ChatState | None = Field(None, description="Null while the account is unauthorized")
    limited_until: datetime | None = Field(
        None, description="Until when the anonymous bot limits the account (state=limited)"
    )
    bots: list[BotStatusOut] = Field(
        [],
        description="Боты аккаунта по приоритету и их доступность; пусто без сервиса чата",
    )


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    direction: Direction
    text: str
    at: datetime
    photo_id: str | None = Field(
        None, description="Фото собеседника. Байты забираются отдельным запросом"
    )
    video_id: str | None = Field(
        None, description="Видео собеседника. Байты забираются отдельным запросом"
    )


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096)


class HistoryResponse(BaseModel):
    state: ChatState
    messages: list[MessageOut]


class ChatAction(StrEnum):
    START = "start"
    STOP = "stop"
    SEND = "send"


class ChatCommand(BaseModel):
    """Incoming WebSocket command."""

    action: ChatAction
    text: str | None = Field(None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def require_text_for_send(self) -> "ChatCommand":
        if self.action is ChatAction.SEND and not self.text:
            raise ValueError("`text` is required for the `send` action")
        return self


class SnapshotOut(BaseModel):
    """Sent once right after the WebSocket connection is accepted."""

    type: Literal["snapshot"] = "snapshot"
    state: ChatState
    messages: list[MessageOut]


class EventOut(BaseModel):
    type: EventType
    state: ChatState
    message: MessageOut | None = None

    @classmethod
    def from_event(cls, event: ChatEvent) -> "EventOut":
        return cls(
            type=event.type,
            state=event.state,
            message=MessageOut.model_validate(event.message) if event.message else None,
        )


class ErrorOut(BaseModel):
    type: Literal["error"] = "error"
    detail: str


class SelftestRequest(BaseModel):
    accounts: list[str] | None = Field(None, description="id аккаунтов; пусто — все")
    bots: list[str] | None = Field(None, description="Боты для проверки; пусто — все")
    dry_run: bool = Field(False, description="Только сессия и флаги, без сообщений ботам")
    text: str = Field("Привет", min_length=1, max_length=4096)
    search_timeout: float = Field(60, ge=1, le=300)
    reply_timeout: float = Field(60, ge=1, le=300)
    close_timeout: float = Field(15, ge=1, le=300)


class SelftestPairOut(BaseModel):
    bot: str
    status: str
    detail: str | None = None
    closed: bool | None = Field(None, description="Бот подтвердил завершение диалога")
    seconds: float = 0


class SelftestAccountOut(BaseModel):
    account: str
    username: str | None = None
    session: str = Field("ok", description="ok или причина проблемы с сессией")
    flags: list[str] = []
    pairs: list[SelftestPairOut]


class SelftestResponse(BaseModel):
    dry_run: bool
    results: list[SelftestAccountOut]
