"""Самопроверка аккаунтов внутри работающего сервиса.

Для каждой пары «аккаунт × анонимный бот»: поиск, собеседник, «Привет», ответ, стоп.
Идёт по уже подключённым Telethon-клиентам, поэтому сессии не копируются. Текст
собеседника нигде не сохраняется и не попадает в результат.
"""

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.config import normalize_bot
from app.services.anon_chat import ProbeBot

logger = logging.getLogger(__name__)

# Статусы пары. closed — отдельное поле: оно не заменяет статус.
ERROR = "error"
BUSY = "busy"
LIMITED = "limited"
SESSION = "session"
SKIPPED = "skipped"
NO_RESPONSE = "no_response"
BOT_RESPONDED = "bot_responded"
PARTNER_FOUND = "partner_found"
REPLY_RECEIVED = "reply_received"

_FLAGS = ("restricted", "scam", "fake", "deleted")

_RUN_LOCK = asyncio.Lock()


def is_running() -> bool:
    return _RUN_LOCK.locked()


@dataclass(slots=True)
class PairResult:
    bot: str
    status: str
    detail: str | None = None
    closed: bool | None = None
    seconds: float = 0.0


@dataclass(slots=True)
class AccountResult:
    account: str
    username: str | None = None
    session: str = "ok"
    flags: list[str] = field(default_factory=list)
    pairs: list[PairResult] = field(default_factory=list)


def _error_detail(exc: BaseException) -> str:
    # Только тип ошибки: текст исключения может содержать лишнее.
    return type(exc).__name__


async def _check_session(account, result: AccountResult) -> None:
    """Заполняет session/flags/username. Сообщений не отправляет."""
    client = account.client
    try:
        if not client.is_connected():
            result.session = "not connected"
            return
        if not await client.is_user_authorized():
            result.session = "not authorized"
            return
        me = await client.get_me()
    except Exception as exc:
        result.session = f"error: {_error_detail(exc)}"
        return
    result.username = getattr(me, "username", None)
    result.flags = [flag for flag in _FLAGS if getattr(me, flag, False)]
    if result.flags:
        result.session = "flags: " + ",".join(result.flags)


def _drain(queue: asyncio.Queue) -> None:
    while not queue.empty():
        queue.get_nowait()


async def _next_text(queue: asyncio.Queue, key: str, deadline: float) -> str | None:
    """Следующее сообщение именно этого бота; чужие пропускаются. None — время вышло."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            username, text = await asyncio.wait_for(queue.get(), timeout=remaining)
        except TimeoutError:
            return None
        if normalize_bot(username) == key:
            return text


async def _probe_pair(
    service,
    bot: ProbeBot,
    queue: asyncio.Queue,
    *,
    text: str,
    search_timeout: float,
    reply_timeout: float,
    close_timeout: float,
) -> PairResult:
    key = normalize_bot(bot.username)
    result = PairResult(bot=bot.username, status=NO_RESPONSE)
    started = time.monotonic()
    searched = False
    left = False
    _drain(queue)
    try:
        await service.probe_send(bot.username, bot.search_command)
        searched = True

        found = False
        responded = False
        deadline = time.monotonic() + search_timeout
        while (message := await _next_text(queue, key, deadline)) is not None:
            if bot.limit_re.search(message):
                result.status = LIMITED
                break
            if bot.found_re.search(message):
                found = True
                break
            if bot.service_re is not None and bot.service_re.search(message):
                continue
            responded = True
        else:
            result.status = BOT_RESPONDED if responded else NO_RESPONSE

        if found:
            result.status = PARTNER_FOUND
            await service.probe_send(bot.username, text)
            deadline = time.monotonic() + reply_timeout
            while (message := await _next_text(queue, key, deadline)) is not None:
                if bot.left_re.search(message):
                    left = True
                    break
                if bot.found_re.search(message) or bot.limit_re.search(message):
                    continue
                if bot.service_re is not None and bot.service_re.search(message):
                    continue
                result.status = REPLY_RECEIVED
                break
    except Exception as exc:
        logger.warning("Самопроверка %s: ошибка %s", bot.username, _error_detail(exc))
        result.status = ERROR
        result.detail = _error_detail(exc)
    finally:
        if searched:
            result.closed = await _close(service, bot, queue, key, left, close_timeout)
        result.seconds = round(time.monotonic() - started, 1)
    return result


async def _close(
    service, bot: ProbeBot, queue: asyncio.Queue, key: str, left: bool, wait: float
) -> bool:
    """Шлёт стоп (ошибки игнорируются) и ждёт подтверждения от бота."""
    try:
        await service.probe_send(bot.username, bot.stop_command)
    except Exception as exc:
        logger.warning("Самопроверка %s: стоп не отправлен (%s)", bot.username, _error_detail(exc))
        return left
    if left:
        return True
    deadline = time.monotonic() + wait
    while (message := await _next_text(queue, key, deadline)) is not None:
        if bot.left_re.search(message):
            return True
        if bot.service_re is not None and bot.service_re.search(message):
            return True
    return False


async def _probe_account(
    account,
    result: AccountResult,
    names: list[str] | None,
    *,
    dry_run: bool,
    text: str,
    search_timeout: float,
    reply_timeout: float,
    close_timeout: float,
) -> None:
    service = account.service
    bots = service.probe_bots(names)
    if result.session != "ok":
        result.pairs = [PairResult(b.username, SESSION, result.session) for b in bots]
        return
    if dry_run:
        result.pairs = [PairResult(b.username, SKIPPED, "dry run") for b in bots]
        return

    queue = await service.begin_probe()
    if queue is None:
        result.pairs = [PairResult(b.username, BUSY, "account is busy") for b in bots]
        return
    try:
        for planned in bots:
            # Статус блокировки берём заново: предыдущая пара могла поймать лимит.
            current = service.probe_bots([planned.username])
            bot = current[0] if current else planned
            if bot.blocked:
                result.pairs.append(PairResult(bot.username, LIMITED, "bot is blocked"))
                continue
            result.pairs.append(
                await _probe_pair(
                    service,
                    bot,
                    queue,
                    text=text,
                    search_timeout=search_timeout,
                    reply_timeout=reply_timeout,
                    close_timeout=close_timeout,
                )
            )
    finally:
        _drain(queue)
        service.end_probe()


async def run_selftest(
    accounts: Iterable,
    *,
    bots: list[str] | None = None,
    dry_run: bool = False,
    text: str = "Привет",
    search_timeout: float = 60,
    reply_timeout: float = 60,
    close_timeout: float = 15,
) -> list[AccountResult]:
    """Последовательно проверяет аккаунты. Один прогон за раз (иначе RuntimeError)."""
    if _RUN_LOCK.locked():
        raise RuntimeError("Selftest is already running")
    results: list[AccountResult] = []
    async with _RUN_LOCK:
        for account in accounts:
            result = AccountResult(account=account.id)
            results.append(result)
            await _check_session(account, result)
            try:
                await _probe_account(
                    account,
                    result,
                    bots,
                    dry_run=dry_run,
                    text=text,
                    search_timeout=search_timeout,
                    reply_timeout=reply_timeout,
                    close_timeout=close_timeout,
                )
            except Exception as exc:
                logger.exception("Самопроверка аккаунта %s прервана", account.id)
                result.pairs.append(PairResult("*", ERROR, _error_detail(exc)))
    return results
