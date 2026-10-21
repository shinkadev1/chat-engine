import logging

from telethon import TelegramClient, events

from app.services.anon_chat import AnonChatService

logger = logging.getLogger(__name__)


def register(client: TelegramClient, *, service: AnonChatService) -> None:
    async def handle_bot_message(event: events.NewMessage.Event) -> None:
        message = event.message
        text = message.message or ""
        kind = _media_kind(message)
        if kind is not None:
            await _forward_media(message, service, kind)
        if text:
            await service.handle_bot_message(text)

    client.add_event_handler(
        handle_bot_message,
        events.NewMessage(from_users=service.bot_username, incoming=True),
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
