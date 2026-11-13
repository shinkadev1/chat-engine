import argparse
import asyncio
import json
import logging
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from app.client import create_client
from app.config import ResolvedAccount, settings
from app.logger import setup_logging

logger = logging.getLogger(__name__)


def _find_account(account_id: str) -> ResolvedAccount:
    for account in settings.resolve_accounts():
        if account.id == account_id:
            return account

    known = ", ".join(account.id for account in settings.resolve_accounts())
    raise SystemExit(f"Unknown account '{account_id}'. Configured: {known}")


def serve() -> None:
    uvicorn.run(
        "app.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


async def _login(account_id: str) -> None:
    account = _find_account(account_id)
    client = create_client(account)

    async with client:
        await client.start(phone=account.phone or (lambda: input("Номер телефона: ")))
        me = await client.get_me()
        logger.info("Account %s authorized as @%s (id=%s)", account.id, me.username, me.id)
        logger.info("Session saved to %s.session", account.session_path)


async def _show_accounts() -> None:
    for account in settings.resolve_accounts():
        client = create_client(account)
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            username = (await client.get_me()).username if authorized else None
        finally:
            await client.disconnect()

        status = f"@{username}" if authorized else "НЕ АВТОРИЗОВАН"
        print(f"{account.id:<16} {status:<24} {account.anon_bot_username}")


def format_message(message) -> str:
    """Одна строка про сообщение: дата, направление, текст до 300 символов, подписи кнопок."""
    direction = "out" if message.out else "in"
    text = (message.message or "").replace("\n", " | ")[:300]
    buttons = [
        getattr(button, "text", "")
        for row in (getattr(message, "buttons", None) or [])
        for button in row
    ]
    suffix = f" buttons={buttons}" if buttons else ""
    return f"{message.date:%Y-%m-%d %H:%M:%S} {direction:<3} {text}{suffix}"


async def dump_messages(client, bot: str, limit: int) -> list[str]:
    """Только чтение истории чата с ботом: ничего не отправляет и не помечает прочитанным."""
    lines = [format_message(m) async for m in client.iter_messages(bot, limit=limit)]
    return lines[::-1]


def _copy_session(account: ResolvedAccount, target: Path) -> ResolvedAccount:
    """Копия сессии: рабочий сервер держит sqlite-файл, а мы его не трогаем."""
    session_file = Path(f"{account.session_path}.session")
    if not session_file.is_file():
        raise SystemExit(f"Account {account.id}: session not found, run: login {account.id}")
    shutil.copy(session_file, f"{target}.session")
    return account.model_copy(update={"session_path": str(target)})


async def _dump_bot(account_id: str, bot: str, limit: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        account = _copy_session(_find_account(account_id), Path(tmp) / "dump")
        client = create_client(account)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise SystemExit(f"Account {account_id} is not authorized")
            for line in await dump_messages(client, bot, limit):
                print(line)
        finally:
            await client.disconnect()


def format_selftest(results: list[dict]) -> list[str]:
    """Таблица результатов самопроверки. Тексты собеседников сюда не попадают."""
    lines: list[str] = []
    for item in results:
        who = f"@{item['username']}" if item.get("username") else "-"
        lines.append(f"{item['account']:<16} {who:<20} session={item.get('session', 'ok')}")
        for pair in item.get("pairs", []):
            closed = {True: "closed", False: "not closed"}.get(pair.get("closed"), "-")
            detail = f" ({pair['detail']})" if pair.get("detail") else ""
            lines.append(
                f"  {pair['bot']:<20} {pair['status']:<15} {closed:<11} "
                f"{pair.get('seconds', 0):>6.1f}s{detail}"
            )
    return lines


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read()).get("detail")
    except (OSError, ValueError, AttributeError):
        return ""
    return f" {detail}" if isinstance(detail, str) and detail else ""


def _selftest(args: argparse.Namespace) -> None:
    base = (settings.selftest_url or f"http://127.0.0.1:{settings.api_port}").rstrip("/")
    payload = {
        "accounts": args.account or None,
        "bots": args.bot or None,
        "dry_run": args.dry_run,
        "text": args.text,
        "search_timeout": args.search_timeout,
        "reply_timeout": args.reply_timeout,
        "close_timeout": args.close_timeout,
    }
    headers = {"Content-Type": "application/json"}
    if settings.api_token:
        headers["Authorization"] = f"Bearer {settings.api_token}"
    request = urllib.request.Request(
        f"{base}/api/v1/selftest",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Selftest failed: HTTP {exc.code}{_http_detail(exc)}") from None
    except OSError as exc:
        raise SystemExit(f"Selftest failed: {type(exc).__name__}") from None
    for line in format_selftest(body["results"]):
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(prog="app", description="Anonymous chat userbot")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run the HTTP/WebSocket server (default)")

    login_parser = subparsers.add_parser("login", help="Authorize one account interactively")
    login_parser.add_argument("account_id")

    subparsers.add_parser("accounts", help="Show configured accounts and their status")

    dump_parser = subparsers.add_parser(
        "dump-bot", help="Print recent messages of a bot chat (read-only, sends nothing)"
    )
    dump_parser.add_argument("account_id")
    dump_parser.add_argument("bot", help="Bot username, e.g. @MessageAnonBot")
    dump_parser.add_argument("--limit", type=int, default=20)

    selftest_parser = subparsers.add_parser(
        "selftest",
        help="Check accounts and bots through the running service (writes to real bots)",
    )
    selftest_parser.add_argument("--account", action="append", help="Account id (repeatable)")
    selftest_parser.add_argument("--bot", action="append", help="Bot username (repeatable)")
    selftest_parser.add_argument("--dry-run", action="store_true", help="Session checks only")
    selftest_parser.add_argument("--text", default="Привет")
    selftest_parser.add_argument("--search-timeout", type=float, default=60)
    selftest_parser.add_argument("--reply-timeout", type=float, default=60)
    selftest_parser.add_argument("--close-timeout", type=float, default=15)

    args = parser.parse_args()
    setup_logging(settings.log_level)

    try:
        match args.command:
            case "login":
                asyncio.run(_login(args.account_id))
            case "dump-bot":
                asyncio.run(_dump_bot(args.account_id, args.bot, args.limit))
            case "selftest":
                _selftest(args)
            case "accounts":
                asyncio.run(_show_accounts())
            case _:
                serve()
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
