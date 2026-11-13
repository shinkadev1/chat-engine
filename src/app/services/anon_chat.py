import asyncio
import logging
import re
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from io import BytesIO
from uuid import uuid4

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.config import ResolvedAccount, Settings, normalize_bot

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
_LIMIT_DURATION_RE = re.compile(r"через\s+(\d{1,3}):([0-5]\d):([0-5]\d)", re.IGNORECASE)
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


def parse_limit_duration(text: str) -> timedelta | None:
    """Достаёт «через ЧЧ:ММ:СС» из текста про дневной лимит анонимного бота."""
    match = _LIMIT_DURATION_RE.search(text)
    if match is None:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


class ChatState(StrEnum):
    IDLE = "idle"
    SEARCHING = "searching"
    CHATTING = "chatting"
    LIMITED = "limited"


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


@dataclass(slots=True)
class _Bot:
    """Анонимный бот аккаунта: его тексты и состояние недоступности (в памяти)."""

    username: str
    search_command: str
    stop_command: str
    found_re: re.Pattern[str]
    left_re: re.Pattern[str]
    limit_re: re.Pattern[str]
    service_re: re.Pattern[str] | None
    reply_timeout: float = 0
    strict: bool = False
    until: datetime | None = None
    reason: str | None = None

    @property
    def key(self) -> str:
        return normalize_bot(self.username)

    def blocked(self, now: datetime) -> bool:
        return self.until is not None and self.until >= now


@dataclass(frozen=True, slots=True)
class BotStatus:
    username: str
    active: bool
    available: bool
    until: datetime | None
    reason: str | None


def _make_bot(
    username: str,
    *,
    search_command: str,
    stop_command: str,
    found: str,
    left: str,
    limit: str,
    service: str | None,
    reply_timeout: float,
    strict: bool = False,
) -> _Bot:
    return _Bot(
        username=username,
        search_command=search_command,
        stop_command=stop_command,
        found_re=re.compile(found, re.IGNORECASE),
        left_re=re.compile(left, re.IGNORECASE),
        limit_re=re.compile(limit, re.IGNORECASE),
        service_re=re.compile(service, re.IGNORECASE) if service else None,
        reply_timeout=reply_timeout,
        strict=strict,
    )


class AnonChatService:
    """Drives anonymous chat bots from a Telegram user account.

    The bot relays messages between two strangers, so the whole dialog happens
    in a single Telegram chat. Service notifications ("partner found", "partner
    left") are told apart from the partner's own messages by regex patterns,
    which keeps the service usable with different anonymous chat bots.

    An account may know several bots in priority order. A search goes to the first
    bot that is not marked unavailable (daily limit, blocked, silent); when it fails
    the next one is tried. Once a partner is found the bot never changes mid-dialog.

    Every state change and every message is published to subscribers, so
    WebSocket clients get updates without polling.
    """

    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        bot_username: str,
        account: ResolvedAccount | None = None,
    ) -> None:
        self._client = client
        self.account_id = account.id if account is not None else "default"
        if account is not None and account.bots:
            self._bots = [
                _make_bot(
                    bot.username,
                    search_command=bot.search_command,
                    stop_command=bot.stop_command,
                    found=bot.partner_found_pattern,
                    left=bot.partner_left_pattern,
                    limit=bot.limit_pattern,
                    service=bot.service_pattern,
                    reply_timeout=bot.reply_timeout_seconds,
                    strict=getattr(bot, "profiled", False),
                )
                for bot in account.bots
            ]
        else:
            # Поля аккаунта (с уже применёнными фолбэками) главнее глобальных настроек.
            source = account or settings
            self._bots = [
                _make_bot(
                    bot_username,
                    search_command=source.anon_search_command,
                    stop_command=source.anon_stop_command,
                    found=source.anon_partner_found_pattern,
                    left=source.anon_partner_left_pattern,
                    limit=source.anon_limit_pattern,
                    service=getattr(source, "anon_service_pattern", None),
                    reply_timeout=0,
                )
            ]
        self._active: _Bot | None = None

        self._limit_default = timedelta(seconds=settings.anon_limit_default_seconds)
        self._unavailable = timedelta(seconds=settings.anon_bot_unavailable_seconds)
        self._limit_task: asyncio.Task[None] | None = None
        self._reply_task: asyncio.Task[None] | None = None

        self._state = ChatState.IDLE
        self._history: deque[ChatMessage] = deque(maxlen=settings.history_limit)
        self._seq = 0
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[ChatEvent]] = set()
        self._files: dict[str, tuple[bytes, str, str]] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _current(self) -> _Bot:
        """Бот, которому принадлежит диалог; до первого поиска — основной."""
        return self._active or self._bots[0]

    def _free_bots(self) -> list[_Bot]:
        now = self._now()
        return [bot for bot in self._bots if not bot.blocked(now)]

    @property
    def bot_username(self) -> str:
        if self._active is not None and self._state in (ChatState.SEARCHING, ChatState.CHATTING):
            return self._active.username
        free = self._free_bots()
        return (free[0] if free else self._bots[0]).username

    def match_bot(self, username: str | None) -> str | None:
        """Имя одного из ботов аккаунта по username отправителя; чужие отправители — None."""
        if not username:
            return None
        key = normalize_bot(username)
        return next((bot.username for bot in self._bots if bot.key == key), None)

    def is_active_bot(self, username: str) -> bool:
        return self._current().key == normalize_bot(username)

    def bots_status(self) -> list[BotStatus]:
        now = self._now()
        working = self._state in (ChatState.SEARCHING, ChatState.CHATTING)
        return [
            BotStatus(
                username=bot.username,
                active=working and bot is self._active,
                available=not bot.blocked(now),
                until=bot.until if bot.blocked(now) else None,
                reason=bot.reason if bot.blocked(now) else None,
            )
            for bot in self._bots
        ]

    @property
    def state(self) -> ChatState:
        return self._state

    @property
    def limited_until(self) -> datetime | None:
        if self._free_bots():
            return None
        untils = [bot.until for bot in self._bots if bot.until is not None]
        return min(untils) if untils else None

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
        """Ask the first available bot for a new partner."""
        async with self._lock:
            if self._state is ChatState.LIMITED and self.limited_until is not None:
                raise ChatError(
                    "Account is limited by the anonymous bots until "
                    f"{self.limited_until.isoformat()}"
                )
            if self._state is ChatState.LIMITED:
                self._set_state(ChatState.IDLE)
            if self._state is not ChatState.IDLE:
                raise ChatError(f"Chat is already active (state={self._state})")

            self._history.clear()
            if not await self._search():
                until = self.limited_until
                raise ChatError(
                    "All anonymous bots are unavailable"
                    + (f" until {until.isoformat()}" if until else "")
                )

        logger.info("Looking for a partner via %s", self.bot_username)
        return self._state

    async def stop_chat(self) -> ChatState:
        """Leave the current dialog or cancel the search."""
        async with self._lock:
            if self._state is ChatState.LIMITED:
                raise ChatError("Account is limited, nothing to stop")
            if self._state is ChatState.IDLE:
                raise ChatError("There is no active chat to stop")

            bot = self._current()
            self._cancel_reply_timer()
            await self._send(bot.stop_command)
            self._set_state(ChatState.IDLE)
            self._record(Direction.SYSTEM, f"→ {bot.stop_command}")

        logger.info("Chat stopped")
        return self._state

    async def send_message(self, text: str) -> ChatMessage:
        """Relay a message to the current partner."""
        async with self._lock:
            if self._state is not ChatState.CHATTING:
                raise ChatError(f"No partner connected (state={self._state})")

            await self._send(text)
            return self._record(Direction.OUTGOING, text)

    async def _search(self) -> bool:
        """Шлёт команду поиска первому свободному боту (под self._lock).

        Недоступный бот получает until, поиск идёт дальше. Нет свободных — LIMITED.
        """
        for bot in self._free_bots():
            try:
                await self._client.send_message(bot.username, bot.search_command)
            except Exception as exc:
                if isinstance(exc, FloodWaitError):
                    self._block(bot, "flood_wait", timedelta(seconds=max(int(exc.seconds), 1)))
                else:
                    self._block(bot, "send_error", self._unavailable)
                logger.warning(
                    "Бот %s недоступен (%s: %s), пробую следующий",
                    bot.username,
                    type(exc).__name__,
                    exc,
                )
                continue
            self._active = bot
            self._set_state(ChatState.SEARCHING)
            self._record(Direction.SYSTEM, f"→ {bot.search_command}")
            self._arm_reply_timer(bot)
            return True
        self._enter_limited()
        return False

    def _block(self, bot: _Bot, reason: str, duration: timedelta) -> None:
        bot.until = self._now() + duration
        bot.reason = reason

    def _enter_limited(self) -> None:
        self._cancel_reply_timer()
        self._set_state(ChatState.LIMITED)
        until = self.limited_until
        if until is None:
            return
        self._restart_limit_timer(max((until - self._now()).total_seconds(), 0))
        logger.warning("All anonymous bots are unavailable, wait until %s", until)

    def _arm_reply_timer(self, bot: _Bot) -> None:
        self._cancel_reply_timer()
        if bot.reply_timeout > 0:
            self._reply_task = asyncio.create_task(self._watch_reply(bot, bot.reply_timeout))

    def _cancel_reply_timer(self) -> None:
        task, self._reply_task = self._reply_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _watch_reply(self, bot: _Bot, seconds: float) -> None:
        await asyncio.sleep(seconds)
        async with self._lock:
            if self._reply_task is not asyncio.current_task():
                return
            self._reply_task = None
            if self._state is not ChatState.SEARCHING or self._active is not bot:
                return
            self._block(bot, "no_reply", self._unavailable)
            logger.warning("Бот %s не ответил за %s с, пробую следующий", bot.username, seconds)
            try:
                await self._client.send_message(bot.username, bot.stop_command)
            except Exception:
                logger.warning("Не удалось отправить %s боту %s", bot.stop_command, bot.username)
            try:
                await self._search()
            except Exception:
                logger.exception("Не удалось перейти к следующему боту")

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
            await self._client.send_file(self._current().username, buffer, force_document=False)
            return self._record(Direction.OUTGOING, "[фото]")

    async def handle_bot_message(self, text: str, bot: str | None = None) -> None:
        """Classify an incoming message from an anonymous chat bot.

        `bot` — username отправителя; без него сообщение считается от текущего бота.
        """
        text = partner_text(text)
        if not text:
            logger.info("Пропускаю подпись к файлу анонимного чата")
            return
        async with self._lock:
            source = self._current()
            if bot is not None:
                found = next((b for b in self._bots if b.key == normalize_bot(bot)), None)
                if found is None:
                    logger.info("Сообщение от неизвестного бота %s пропущено", bot)
                    return
                source = found

            if source is not self._current():
                await self._handle_other_bot(source, text)
                return

            if self._state is ChatState.SEARCHING:
                self._cancel_reply_timer()

            if self._state is not ChatState.CHATTING and source.limit_re.search(text):
                self._record(Direction.SYSTEM, text)
                duration = parse_limit_duration(text)
                if duration is None:
                    duration = self._limit_default
                self._block(source, "limit", duration)
                logger.warning("Bot %s daily limit, wait until %s", source.username, source.until)
                if self._state is ChatState.SEARCHING:
                    await self._search()
                elif not self._free_bots():
                    self._enter_limited()
            elif not (
                source.strict and self._state is ChatState.CHATTING
            ) and source.found_re.search(text):
                self._set_state(ChatState.CHATTING)
                self._record(Direction.SYSTEM, text)
                logger.info("Partner found via %s", source.username)
            elif source.left_re.search(text):
                self._set_state(ChatState.IDLE)
                self._record(Direction.SYSTEM, text)
                logger.info("Partner left")
            elif source.service_re is not None and source.service_re.search(text):
                logger.info("Пропускаю служебное сообщение анонимного бота")
            elif self._state is ChatState.CHATTING:
                self._record(Direction.INCOMING, text)
            else:
                self._record(Direction.SYSTEM, text)

    async def _handle_other_bot(self, source: _Bot, text: str) -> None:
        """Сообщение бота, который сейчас не ведёт диалог: в историю не попадает."""
        if source.limit_re.search(text):
            duration = parse_limit_duration(text)
            self._block(source, "limit", self._limit_default if duration is None else duration)
            logger.warning("Bot %s (inactive) limit, until %s", source.username, source.until)
            if self._state is ChatState.IDLE and not self._free_bots():
                self._enter_limited()
        else:
            logger.info("Пропускаю сообщение неактивного бота %s", source.username)

    def _restart_limit_timer(self, seconds: float) -> None:
        if self._limit_task is not None and self._limit_task is not asyncio.current_task():
            self._limit_task.cancel()
        self._limit_task = asyncio.create_task(self._release_limit(seconds))

    async def _release_limit(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        async with self._lock:
            if self._state is not ChatState.LIMITED:
                return
            now = self._now()
            for bot in self._bots:
                if bot.until is not None and bot.until <= now:
                    bot.until = None
                    bot.reason = None
            until = self.limited_until
            if until is not None:
                self._restart_limit_timer(max((until - now).total_seconds(), 0))
                return
            self._set_state(ChatState.IDLE)
            logger.info("Anonymous bot limit is over")

    async def shutdown(self) -> None:
        self._cancel_reply_timer()
        if self._limit_task is not None:
            self._limit_task.cancel()
            self._limit_task = None
        if self._state in (ChatState.IDLE, ChatState.LIMITED):
            return
        try:
            await self._send(self._current().stop_command)
        except Exception:
            logger.exception("Failed to leave the chat on shutdown")
        self._set_state(ChatState.IDLE)

    async def _send(self, text: str) -> None:
        await self._client.send_message(self._current().username, text)

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
