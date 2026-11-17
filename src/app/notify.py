"""Отправка текста в Telegram через Bot API (stdlib). Токен и URL в логи не попадают."""

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

MAX_LENGTH = 4096


def split_text(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """Режет по строкам, чтобы каждая часть была не длиннее limit."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current or not parts:
        parts.append(current)
    return parts


def _post(token: str, chat_id: str, text: str, timeout: float) -> int:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def send_sync(
    token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 15,
    retries: int = 2,
    delay: float = 2,
) -> bool:
    for part in split_text(text):
        for attempt in range(retries + 1):
            try:
                status = _post(token, chat_id, part, timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
            except (OSError, ValueError) as exc:
                logger.warning("Отчёт не отправлен: %s", type(exc).__name__)
                status = None
            else:
                if 200 <= status < 300:
                    logger.info("Отчёт отправлен: HTTP %s", status)
                    break
            if status is not None:
                logger.warning("Отчёт не отправлен: HTTP %s", status)
            if attempt < retries:
                time.sleep(delay)
        else:
            return False
    return True


async def send_telegram(token: str, chat_id: str, text: str, **kwargs) -> bool:
    return await asyncio.to_thread(send_sync, token, chat_id, text, **kwargs)
