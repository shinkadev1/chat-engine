"""Лёгкая проверка аккаунтов и ботов для периодического отчёта в Telegram.

Не пишет незнакомым: занятые аккаунты не трогаем, у свободных шлём только стоп-команду
бота (вне диалога она безвредна) и ждём любой ответ. Дневные лимиты не расходуются.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from app import selftest
from app.config import Settings, normalize_bot
from app.notify import send_telegram
from app.selftest import AccountResult, _check_session, _drain, _next_text
from app.services.anon_chat import ChatState

logger = logging.getLogger(__name__)

OK = "+"
FAIL = "-"
LIMIT = "лимит"
UNKNOWN = "?"

FIRST_RUN_DELAY = 300.0
PROBE_WAIT = 20.0

_SESSION_REASONS = {
    "not connected": "нет соединения",
    "not authorized": "не авторизован",
}


@dataclass(slots=True)
class BotHealth:
    bot: str
    mark: str
    detail: str | None = None


@dataclass(slots=True)
class AccountHealth:
    account: str
    active: bool = True
    reason: str | None = None
    busy: bool = False
    bots: list[BotHealth] = field(default_factory=list)


@dataclass(slots=True)
class HealthReport:
    text: str
    accounts: list[AccountHealth]
    active_accounts: int
    total_accounts: int
    working: int
    total: int
    notified: bool = False


def _session_reason(session: str) -> str:
    if session in _SESSION_REASONS:
        return _SESSION_REASONS[session]
    if session.startswith("flags:"):
        return "ограничен:" + session.removeprefix("flags:")
    return "ошибка сессии"


def _moscow(now: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("Europe/Moscow"))
    except Exception:  # нет tzdata в slim-образе
        return now.astimezone(timezone(timedelta(hours=3)))


async def _probe_bot(service, queue, bot, wait: float) -> BotHealth:
    key = normalize_bot(bot.username)
    _drain(queue)
    try:
        await service.probe_send(bot.username, bot.stop_command)
    except Exception as exc:
        return BotHealth(bot.username, FAIL, type(exc).__name__)
    message = await _next_text(queue, key, time.monotonic() + wait)
    if message is None:
        return BotHealth(bot.username, FAIL, "no_reply")
    if bot.limit_re.search(message):
        return BotHealth(bot.username, LIMIT)
    return BotHealth(bot.username, OK)


def _blocked_mark(status) -> BotHealth:
    if status.reason == "limit":
        return BotHealth(status.username, LIMIT)
    return BotHealth(status.username, FAIL, status.reason)


async def _check_account(account, window: float, wait: float) -> AccountHealth:
    result = AccountResult(account=account.id)
    await _check_session(account, result)
    service = account.service
    health = AccountHealth(account=account.id)
    if result.session != "ok":
        health.active = False
        health.reason = _session_reason(result.session)
        health.bots = [BotHealth(b.username, FAIL, "inactive") for b in service.probe_bots()]
        return health

    now = datetime.now(UTC)
    recent = timedelta(seconds=window)
    queue = None
    working = service.state in (ChatState.SEARCHING, ChatState.CHATTING)
    if not working:
        queue = await service.begin_probe(press_prompt=False)
    health.busy = queue is None and service.state is not ChatState.LIMITED
    try:
        for status in service.bots_status():
            if not status.available:
                health.bots.append(_blocked_mark(status))
                continue
            found = service.last_partner_found_at(status.username)
            if status.active or (found is not None and now - found <= recent):
                health.bots.append(BotHealth(status.username, OK, "recent"))
            elif queue is None:
                health.bots.append(BotHealth(status.username, UNKNOWN, "busy"))
            else:
                # Свежий статус: предыдущая проба могла поймать лимит.
                probe = service.probe_bots([status.username])[0]
                if probe.blocked:
                    health.bots.append(BotHealth(status.username, LIMIT))
                else:
                    health.bots.append(await _probe_bot(service, queue, probe, wait))
    finally:
        if queue is not None:
            _drain(queue)
            service.end_probe()
    return health


def format_report(accounts: list[AccountHealth], when: datetime) -> tuple[str, int, int, int, int]:
    """Текст сводки и счётчики: активных/всего аккаунтов, работающих/всех пар.

    «Работает» — отметка «+» у бота активного аккаунта; в знаменатель входят пары всех
    аккаунтов, боты неактивных считаются неработающими.
    """
    local = _moscow(when)
    active = [a for a in accounts if a.active]
    total = sum(len(a.bots) for a in accounts)
    working = sum(1 for a in active for b in a.bots if b.mark == OK)
    lines = [
        f"Проверка chat-engine {local:%d.%m %H:%M} (МСК)",
        f"Аккаунтов активно: {len(active)}/{len(accounts)}, чатов работает: {working}/{total}",
    ]
    for item in accounts:
        if not item.active:
            lines.append(f"{item.account} -> не активен ({item.reason})")
            continue
        bots = ", ".join(f"{b.bot.lstrip('@')}({b.mark})" for b in item.bots)
        note = " (в диалоге)" if item.busy else ""
        lines.append(f"{item.account} -> {bots}{note}")
    return "\n".join(lines), len(active), len(accounts), working, total


async def run_healthcheck(
    accounts: Iterable,
    *,
    interval_hours: float = 4,
    probe_wait: float | None = None,
    now: datetime | None = None,
) -> HealthReport:
    """Один лёгкий прогон под общим замком самопроверки (занят — SelftestBusyError)."""
    if selftest._RUN_LOCK.locked():
        raise selftest.SelftestBusyError("Selftest is already running")
    window = 2 * interval_hours * 3600
    results: list[AccountHealth] = []
    async with selftest._RUN_LOCK:
        for account in accounts:
            try:
                results.append(await _check_account(account, window, probe_wait or PROBE_WAIT))
            except Exception as exc:
                logger.exception("Проверка аккаунта %s прервана", account.id)
                results.append(AccountHealth(account.id, False, type(exc).__name__))
    text, active, total_accounts, working, total = format_report(results, now or datetime.now(UTC))
    return HealthReport(text, results, active, total_accounts, working, total)


async def notify_report(settings: Settings, report: HealthReport) -> bool:
    token, chat = settings.selftest_notify_bot_token, settings.selftest_notify_chat_id
    if token is None or not chat:
        return False
    report.notified = await send_telegram(token.get_secret_value(), chat, report.text)
    return report.notified


def scheduler_enabled(settings: Settings) -> bool:
    return (
        settings.selftest_interval_hours > 0
        and settings.selftest_notify_bot_token is not None
        and bool(settings.selftest_notify_chat_id)
    )


async def _scheduler(manager, settings: Settings, first_delay: float) -> None:
    interval = settings.selftest_interval_hours
    await asyncio.sleep(first_delay)
    while True:
        try:
            report = await run_healthcheck(manager.list(), interval_hours=interval)
            await notify_report(settings, report)
        except asyncio.CancelledError:
            raise
        except selftest.SelftestBusyError:
            logger.info("Плановая проверка пропущена: идёт самопроверка")
        except Exception as exc:
            logger.error("Плановая проверка не удалась: %s", type(exc).__name__)
        await asyncio.sleep(interval * 3600)


def start_scheduler(manager, settings: Settings, first_delay: float = FIRST_RUN_DELAY):
    """Фоновая задача или None, если выключено (интервал 0 либо нет токена/чата)."""
    if not scheduler_enabled(settings):
        return None
    return asyncio.create_task(_scheduler(manager, settings, first_delay), name="healthcheck")


async def stop_scheduler(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
