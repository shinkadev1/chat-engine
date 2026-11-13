import logging

from telethon import TelegramClient, events

from app.services.anon_chat import AnonChatService

logger = logging.getLogger(__name__)


def register(client: TelegramClient, *, service: AnonChatService) -> None:
    async def handle_bot_message(event: events.NewMessage.Event) -> None:
        bot = await _bot_of(event, service)
        if bot is None:
            return
        message = event.message
        text = message.message or ""
        _log_incoming(service, message, "new", bot)
        kind = _media_kind(message)
        # Файлы пересылаем только от бота, который ведёт диалог.
        if kind is not None and not service.probing and service.is_active_bot(bot):
            await _forward_media(message, service, kind)
        if text:
            await service.handle_bot_message(text, bot)

    async def log_bot_edit(event: events.MessageEdited.Event) -> None:
        # Только диагностика: правки сообщений бота в диалог не попадают.
        bot = await _bot_of(event, service)
        if bot is not None:
            _log_incoming(service, event.message, "edit", bot)

    # Без from_users: у несуществующего username Telethon не может его разрешить,
    # и обработчик не работал бы вовсе. Отправителя фильтруем сами.
    private = {"incoming": True, "func": lambda event: event.is_private}
    client.add_event_handler(handle_bot_message, events.NewMessage(**private))
    client.add_event_handler(log_bot_edit, events.MessageEdited(**private))


async def _bot_of(event, service: AnonChatService) -> str | None:
    """Имя бота аккаунта, приславшего событие; остальные собеседники — None."""
    try:
        sender = await event.get_sender()
    except Exception:
        logger.debug("Не удалось получить отправителя", exc_info=True)
        return None
    matched = service.match_bot(getattr(sender, "username", None))
    if matched is not None and not getattr(sender, "bot", False):
        logger.warning("Игнорирую не-бота с username бота %s", matched)
        return None
    return matched


def _log_incoming(service: AnonChatService, message, source: str, bot: str) -> None:
    """Диагностика формата сообщений анонимного бота: тип медиа и начало текста."""
    media = type(message.media).__name__ if message.media is not None else "-"
    # Текст собеседника в диалоге не пишем — только служебные сообщения вне диалога.
    raw = message.message or ""
    if service.state == "chatting" or service.probing:
        text = f"<{len(raw)} симв.>"
    else:
        text = raw.replace("\n", " | ")[:160]
    logger.info(
        "Бот %s -> %s [%s] id=%s state=%s media=%s photo=%s buttons=%s text=%r",
        bot,
        service.account_id,
        source,
        message.id,
        service.state,
        media,
        bool(message.photo),
        message.reply_markup is not None,
        text,
    )


def _media_kind(message) -> str | None:
    """Фото и видео анонимного бота. Стикер и голос сюда не входят."""
    if message.sticker:
        return None
    if message.photo:
        return "photo"
    if message.video or message.video_note or message.gif:
        return "video"
    mime = message.file.mime_type if message.file is not None else None
    if mime and mime.startswith("video/"):
        return "video"
    return None


async def _forward_media(message, service: AnonChatService, kind: str) -> None:
    """Скачивает сам файл. Подпись «нажмите на фото/видео» в диалог не попадает."""
    label = "видео" if kind == "video" else "фото"
    try:
        downloaded = await message.download_media(file=bytes)
    except Exception:
        logger.exception("Не скачал %s из анонимного чата", label)
        return
    if not isinstance(downloaded, bytes | bytearray) or not downloaded:
        logger.warning("Анонимный чат прислал %s без байтов", label)
        return
    mime = "video/mp4" if kind == "video" else "image/jpeg"
    if message.file is not None and message.file.mime_type:
        mime = message.file.mime_type
    await service.handle_bot_media(bytes(downloaded), mime, kind)
