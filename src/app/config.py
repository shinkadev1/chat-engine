import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AccountConfig(BaseModel):
    """One Telegram account as declared by the user."""

    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,32}$")
    api_id: int | None = None
    api_hash: str | None = None
    session_name: str | None = Field(None, description="Defaults to <SESSIONS_DIR>/<id>")
    phone: str | None = Field(None, description="Pre-fills the number during `login`")
    anon_bot_username: str | None = None
    mode: Literal["normal", "adult"] = Field(
        "normal", description="normal — обычный анонимный чат, adult — «пошлый чат» (premium)"
    )
    # Необязательные переопределения; без них берутся глобальные ANON_* из Settings.
    anon_search_command: str | None = None
    anon_stop_command: str | None = None
    anon_partner_found_pattern: str | None = None
    anon_partner_left_pattern: str | None = None
    anon_limit_pattern: str | None = None
    anon_service_pattern: str | None = Field(
        None, description="Служебные/рекламные сообщения бота: не попадают в диалог"
    )

    @field_validator(
        "anon_partner_found_pattern",
        "anon_partner_left_pattern",
        "anon_limit_pattern",
        "anon_service_pattern",
    )
    @classmethod
    def _compilable(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                account = info.data.get("id", "?")
                raise ValueError(f"Account {account!r}: bad {info.field_name}: {exc}") from exc
        return value


class ResolvedAccount(BaseModel):
    """Account config with every fallback already applied."""

    id: str
    api_id: int
    api_hash: str
    session_path: str
    phone: str | None
    anon_bot_username: str
    mode: Literal["normal", "adult"] = "normal"
    anon_search_command: str = "/search"
    anon_stop_command: str = "/stop"
    anon_partner_found_pattern: str = ""
    anon_partner_left_pattern: str = ""
    anon_limit_pattern: str = ""
    anon_service_pattern: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_id: int | None = Field(
        None, description="Default Telegram API ID for accounts that do not set their own"
    )
    api_hash: str | None = Field(
        None, description="Default Telegram API hash for accounts that do not set their own"
    )

    sessions_dir: Path = Path("sessions")
    accounts_file: Path | None = Field(None, description="JSON file with a list of accounts")
    accounts: list[AccountConfig] = []

    log_level: str = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: str | None = Field(
        None, description="Bearer token required by /api/v1; auth is disabled when empty"
    )
    cors_origins: list[str] = ["*"]

    anon_bot_username: str = Field(
        "@AnonRuBot",
        description="Default anonymous chat bot for accounts that do not override it",
    )
    anon_search_command: str = "/search"
    anon_stop_command: str = "/stop"
    anon_partner_found_pattern: str = (
        r"собеседник найден|найден собеседник|partner found|chat started"
    )
    anon_partner_left_pattern: str = (
        r"закончил(?:а|и)? с вами связь|собеседник (покинул|вышел|отключ|закончил)|"
        r"диалог (завершен|завершён|окончен)|partner left|chat (ended|stopped)"
    )

    anon_limit_pattern: str = r"исчерпали дневной лимит|лимит на чаты будет снят через"
    anon_limit_default_seconds: int = Field(
        3600, ge=60, description="Limit duration when the bot text has no time to wait"
    )

    history_limit: int = Field(200, ge=1, description="Max messages kept in memory per chat")

    def account_configs(self) -> list[AccountConfig]:
        if self.accounts_file is not None:
            if not self.accounts_file.is_file():
                raise FileNotFoundError(f"ACCOUNTS_FILE not found: {self.accounts_file}")
            payload = json.loads(self.accounts_file.read_text(encoding="utf-8"))
            return [AccountConfig.model_validate(item) for item in payload]

        if self.accounts:
            return self.accounts

        return [AccountConfig(id="default")]

    def resolve_accounts(self) -> list[ResolvedAccount]:
        resolved: list[ResolvedAccount] = []
        seen: set[str] = set()

        for config in self.account_configs():
            if config.id in seen:
                raise ValueError(f"Duplicate account id: {config.id}")
            seen.add(config.id)

            api_id = config.api_id or self.api_id
            api_hash = config.api_hash or self.api_hash
            if not api_id or not api_hash:
                raise ValueError(
                    f"Account {config.id!r}: set api_id and api_hash in the accounts file "
                    "or API_ID and API_HASH in .env"
                )

            session_path = config.session_name or str(self.sessions_dir / config.id)
            resolved.append(
                ResolvedAccount(
                    id=config.id,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_path=session_path,
                    phone=config.phone,
                    anon_bot_username=config.anon_bot_username or self.anon_bot_username,
                    mode=config.mode,
                    anon_search_command=config.anon_search_command or self.anon_search_command,
                    anon_stop_command=config.anon_stop_command or self.anon_stop_command,
                    anon_partner_found_pattern=(
                        config.anon_partner_found_pattern or self.anon_partner_found_pattern
                    ),
                    anon_partner_left_pattern=(
                        config.anon_partner_left_pattern or self.anon_partner_left_pattern
                    ),
                    anon_limit_pattern=config.anon_limit_pattern or self.anon_limit_pattern,
                    anon_service_pattern=config.anon_service_pattern,
                )
            )

        if not resolved:
            raise ValueError("No accounts configured")
        return resolved


settings = Settings()
