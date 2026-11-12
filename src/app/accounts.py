import logging
from dataclasses import dataclass, field

from telethon import TelegramClient

from app.client import create_client
from app.config import ResolvedAccount, Settings
from app.handlers import register_handlers
from app.services.anon_chat import AnonChatService

logger = logging.getLogger(__name__)


class UnknownAccountError(KeyError):
    """No account with the requested id is configured."""


@dataclass(slots=True)
class Account:
    config: ResolvedAccount
    client: TelegramClient
    service: AnonChatService
    authorized: bool = field(default=False)
    username: str | None = field(default=None)

    @property
    def id(self) -> str:
        return self.config.id


class AccountManager:
    """Owns one Telethon client per configured account.

    An account whose session is missing or revoked stays registered but
    unauthorized: the server must not block on an interactive login prompt,
    which is impossible inside a container.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._accounts: dict[str, Account] = {}

    def __len__(self) -> int:
        return len(self._accounts)

    def list(self) -> list[Account]:
        return list(self._accounts.values())

    def get(self, account_id: str) -> Account:
        try:
            return self._accounts[account_id]
        except KeyError:
            raise UnknownAccountError(account_id) from None

    async def start(self) -> None:
        for config in self._settings.resolve_accounts():
            await self._start_account(config)

        authorized = sum(account.authorized for account in self._accounts.values())
        logger.info("Accounts ready: %s of %s authorized", authorized, len(self._accounts))

    async def stop(self) -> None:
        for account in self._accounts.values():
            try:
                await account.service.shutdown()
            finally:
                await account.client.disconnect()

    async def _start_account(self, config: ResolvedAccount) -> None:
        client = create_client(config)
        service = AnonChatService(
            client, self._settings, bot_username=config.anon_bot_username, account=config
        )
        register_handlers(client, service=service)

        account = Account(config=config, client=client, service=service)
        self._accounts[config.id] = account

        try:
            await client.connect()
        except Exception:
            logger.exception("Account %s: connection failed", config.id)
            return

        if not await client.is_user_authorized():
            logger.warning(
                "Account %s is not authorized, run: python -m app login %s",
                config.id,
                config.id,
            )
            return

        me = await client.get_me()
        account.authorized = True
        account.username = me.username or me.first_name
        logger.info("Account %s started as @%s (id=%s)", config.id, account.username, me.id)
