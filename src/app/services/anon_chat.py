import asyncio
import logging
import re
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO
from uuid import uuid4

from telethon import TelegramClient

from app.config import Settings

logger = logging.getLogger(__name__)

# Анонимный бот прячет чужой файл за этой подписью. В наш диалог уходит сам файл.
_NOTICE_LINE_RE = re.compile(
    r"хочет отправить вам (?:фото|видео)|"
    r"нажмите на (?:фото|видео)|"
    r"не скрывать для этого диалога|"
    r"^подпись к (?:фото|видео)\s*:?\s*$",
    re.IGNORECASE,
)
_CAPTION_PREFIX_RE = re.compile(r"^подпись к (?:фото|видео)\s*:\s*", re.IGNORECASE)
_MAX_PHOTO_BYTES = 20 * 1024 * 1024
_MAX_VIDEO_BYTES = 100 * 1024 * 1024
_MAX_PENDING_FILES = 8


def partner_text(text: str) -> str:
    """Убирает служебную подпись анонимного бота. Пустая строка в диалог не попадает."""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = _CAPTION_PREFIX_RE.sub("", line.strip()).strip()
        if not stripped or _NOTICE_LINE_RE.search(stripped):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


class ChatState(StrEnum):
    IDLE = "idle"
    SEARCHING = "searching"
    CHATTING = "chatting"


class Direction(StrEnum):
    INCOMING = "in"
    OUTGOING = "out"
    SYSTEM = "system"


class EventType(StrEnum):
    STATE = "state"
    MESSAGE = "message"


class ChatError(RuntimeError):
    """Action is not allowed in the current chat state."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    seq: int
    direction: Direction
    text: str
    at: datetime
    photo_id: str | None = None
    video_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatEvent:
    type: EventType
    state: ChatState
    message: ChatMessage | None = None


class AnonChatService:
    """Drives an anonymous chat bot from a Telegram user account.

    The bot relays messages between two strangers, so the whole dialog happens
    in a single Telegram chat. Service notifications ("partner found", "partner
    left") are told apart from the partner's own messages by regex patterns,
    which keeps the service usable with different anonymous chat bots.

    Every state change and every message is published to subscribers, so
    WebSocket clients get updates without polling.
    """

    def __init__(self, client: TelegramClient, settings: Settings, bot_username: str) -> None:
        self._client = client
        self._bot_username = bot_username
        self._search_command = settings.anon_search_command
        self._stop_command = settings.anon_stop_command
        self._found_re = re.compile(settings.anon_partner_found_pattern, re.IGNORECASE)
        self._left_re = re.compile(settings.anon_partner_left_pattern, re.IGNORECASE)

        self._state = ChatState.IDLE
        self._history: deque[ChatMessage] = deque(maxlen=settings.history_limit)
        self._seq = 0
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[ChatEvent]] = set()
        self._files: dict[str, tuple[bytes, str, str]] = {}

    @property
    def bot_username(self) -> str:
        return self._bot_username

    @property
    def state(self) -> ChatState:
        return self._state

    @property
    def last_seq(self) -> int:
        return self._seq

    def history(self, *, after_seq: int = 0, limit: int | None = None) -> list[ChatMessage]:
        messages = [message for message in self._history if message.seq > after_seq]
        return messages[-limit:] if limit is not None else messages

    @contextmanager
    def subscribe(self, *, maxsize: int = 100) -> Iterator[asyncio.Queue[ChatEvent]]:
        """Register a queue that receives every chat event until the caller leaves."""
        queue: asyncio.Queue[ChatEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    async def start_chat(self) -> ChatState:
        """Ask the bot for a new partner."""
        async with self._lock:
            if self._state is not ChatState.IDLE:
                raise ChatError(f"Chat is already active (state={self._state})")

            self._history.clear()
            await self._send(self._search_command)
            self._set_state(ChatState.SEARCHING)
            self._record(Direction.SYSTEM, f"→ {self._search_command}")

        logger.info("Looking for a partner via %s", self._bot_username)
        return self._state

    async def stop_chat(self) -> ChatState:
        """Leave the current dialog or cancel the search."""
        async with self._lock:
            if self._state is ChatState.IDLE:
                raise ChatError("There is no active chat to stop")

            await self._send(self._stop_command)
            self._set_state(ChatState.IDLE)
            self._record(Direction.SYSTEM, f"→ {self._stop_command}")

        logger.info("Chat stopped")
        return self._state

    async def send_message(self, text: str) -> ChatMessage:
        """Relay a message to the current partner."""
        async with self._lock:
            if self._state is not ChatState.CHATTING:
                raise ChatError(f"No partner connected (state={self._state})")

            await self._send(text)
            return self._record(Direction.OUTGOING, text)

    async def handle_bot_photo(self, data: bytes, content_type: str) -> None:
        """Кладёт фото собеседника в очередь, чтобы мост отдал файл человеку."""
        await self.handle_bot_media(data, content_type, "photo")

    async def handle_bot_media(self, data: bytes, content_type: str, kind: str) -> None:
        """Кладёт фото или видео собеседника в очередь. Мост забирает байты и отдаёт их человеку."""
        limit = _MAX_VIDEO_BYTES if kind == "video" else _MAX_PHOTO_BYTES
        if kind not in ("photo", "video") or not data or len(data) > limit:
            logger.warning("Файл %s размером %s байт не передаю", kind, len(data) if data else 0)
            return
        if kind == "photo" and not content_type.startswith("image/"):
            content_type = "image/jpeg"
        if kind == "video" and not content_type.startswith("video/"):
            content_type = "video/mp4"
        file_id = uuid4().hex
        self._files[file_id] = (data, content_type, kind)
        while len(self._files) > _MAX_PENDING_FILES:
            self._files.pop(next(iter(self._files)))
        async with self._lock:
            if self._state is not ChatState.CHATTING:
                self._files.pop(file_id, None)
                logger.info("Файл пришёл вне диалога")
                return
            if kind == "photo":
                self._record_photo(file_id)
            else:
                self._record_video(file_id)
        logger.info("%s собеседника, %s байт", "Видео" if kind == "video" else "Фото", len(data))

    def take_photo(self, photo_id: str) -> tuple[bytes, str] | None:
        """Отдаёт байты один раз. Повторный запрос уже ничего не найдёт."""
        return self._take_file(photo_id, "photo")

    def take_video(self, video_id: str) -> tuple[bytes, str] | None:
        """Отдаёт байты один раз. Повторный запрос уже ничего не найдёт."""
        return self._take_file(video_id, "video")

    def _take_file(self, file_id: str, kind: str) -> tuple[bytes, str] | None:
        found = self._files.get(file_id)
        if found is None or found[2] != kind:
            return None
        del self._files[file_id]
        return found[0], found[1]

    async def send_photo(self, data: bytes) -> ChatMessage:
        """Отправляет фото в анонимный чат как файл, а не подписью."""
        async with self._lock:
            if self._state is not ChatState.CHATTING:
                raise ChatError(f"No partner connected (state={self._state})")
            if not data or len(data) > _MAX_PHOTO_BYTES:
                raise ChatError("Photo is empty or larger than 20 MB")
            buffer = BytesIO(data)
            buffer.name = "photo.jpg"
            await self._client.send_file(self._bot_username, buffer, force_document=False)
            return self._record(Direction.OUTGOING, "[фото]")

    async def handle_bot_message(self, text: str) -> None:
        """Classify an incoming message from the anonymous chat bot."""
        text = partner_text(text)
        if not text:
            logger.info("Пропускаю подпись к файлу анонимного чата")
            return
        async with self._lock:
            if self._found_re.search(text):
                self._set_state(ChatState.CHATTING)
                self._record(Direction.SYSTEM, text)
                logger.info("Partner found")
            elif self._left_re.search(text):
                self._set_state(ChatState.IDLE)
                self._record(Direction.SYSTEM, text)
                logger.info("Partner left")
            elif self._state is ChatState.CHATTING:
                self._record(Direction.INCOMING, text)
            else:
                self._record(Direction.SYSTEM, text)

    async def shutdown(self) -> None:
        if self._state is ChatState.IDLE:
            return
        try:
            await self._send(self._stop_command)
        except Exception:
            logger.exception("Failed to leave the chat on shutdown")
        self._set_state(ChatState.IDLE)

    async def _send(self, text: str) -> None:
        await self._client.send_message(self._bot_username, text)

    def _set_state(self, state: ChatState) -> None:
        if state is self._state:
            return
        self._state = state
        self._publish(ChatEvent(type=EventType.STATE, state=state))

    def _record(self, direction: Direction, text: str) -> ChatMessage:
        return self._remember(direction, text, None, None)

    def _record_photo(self, photo_id: str) -> ChatMessage:
        return self._remember(Direction.INCOMING, "", photo_id, None)

    def _record_video(self, video_id: str) -> ChatMessage:
        return self._remember(Direction.INCOMING, "", None, video_id)

    def _remember(
        self,
        direction: Direction,
        text: str,
        photo_id: str | None,
        video_id: str | None,
    ) -> ChatMessage:
        self._seq += 1
        message = ChatMessage(
            seq=self._seq,
            direction=direction,
            text=text,
            at=datetime.now(UTC),
            photo_id=photo_id,
            video_id=video_id,
        )
        self._history.append(message)
        self._publish(ChatEvent(type=EventType.MESSAGE, state=self._state, message=message))
        return message

    def _publish(self, event: ChatEvent) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue is full, dropping %s event", event.type)
