import logging

from telethon import TelegramClient, events

from app.services.anon_chat import AnonChatService, is_photo_notice

logger = logging.getLogger(__name__)


def register(client: TelegramClient, *, service: AnonChatService) -> None:
    async def handle_bot_message(event: events.NewMessage.Event) -> None:
        message = event.message
        text = message.message or ""
        if message.photo and not message.sticker:
            await _forward_photo(message, service)
        if text and not is_photo_notice(text):
            await service.handle_bot_message(text)

    client.add_event_handler(
        handle_bot_message,
        events.NewMessage(from_users=service.bot_username, incoming=True),
    )


async def _forward_photo(message, service: AnonChatService) -> None:
    """Скачивает само фото. Подпись «нажмите на фото» в диалог не попадает."""
    try:
        downloaded = await message.download_media(file=bytes)
    except Exception:
        logger.exception("Не скачал фото из анонимного чата")
        return
    if not isinstance(downloaded, bytes | bytearray) or not downloaded:
        logger.warning("Анонимный чат прислал фото без байтов")
        return
    mime = "image/jpeg"
    if message.file is not None and message.file.mime_type:
        mime = message.file.mime_type
    await service.handle_bot_photo(bytes(downloaded), mime)
