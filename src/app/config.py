import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _check_regex(value: str | None, owner: str, field: str | None) -> str | None:
    if value:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"{owner}: bad {field}: {exc}") from exc
    return value


def normalize_bot(username: str) -> str:
    """Имя бота без «@» и регистра: так боты сравниваются между собой."""
    return username.strip().lstrip("@").lower()


class BotProfile(BaseModel):
    """Команды и паттерны одного анонимного бота. Пустые поля берутся из следующего слоя."""

    username: str
    search_command: str | None = None
    stop_command: str | None = None
    partner_found_pattern: str | None = None
    partner_left_pattern: str | None = None
    stop_ack_pattern: str | None = Field(
        None, description="Подтверждение нашего стопа: диалог не считается оборванным собеседником"
    )
    limit_pattern: str | None = None
    service_pattern: str | None = None
    reply_timeout_seconds: int | None = Field(
        None, ge=0, description="Сколько ждать ответ бота на поиск; 0 — не ждать"
    )

    @field_validator(
        "partner_found_pattern",
        "partner_left_pattern",
        "stop_ack_pattern",
        "limit_pattern",
        "service_pattern",
    )
    @classmethod
    def _compilable(cls, value: str | None, info: ValidationInfo) -> str | None:
        owner = f"Bot {info.data.get('username', '?')!r}"
        return _check_regex(value, owner, info.field_name)


class ResolvedBot(BaseModel):
    """Бот аккаунта со всеми подстановками."""

    username: str
    search_command: str = "/search"
    stop_command: str = "/stop"
    partner_found_pattern: str = ""
    partner_left_pattern: str = ""
    stop_ack_pattern: str | None = None
    limit_pattern: str = ""
    service_pattern: str | None = None
    reply_timeout_seconds: float = 0
    # Есть профиль (встроенный или заданный): паттерны уточнялись под этого бота.
    profiled: bool = False


# Паттерны запасных ботов построены по реальным текстам, снятым с прод-аккаунтов.
# Якорь `^\W*` пропускает эмодзи в начале и не даёт сработать на слова собеседника
# в середине фразы; где бот ставит текст не в начало, берётся характерная фраза целиком.
# Точные тексты уточняются через `dump-bot` и правятся ANON_BOT_PROFILES или
# accounts.json без релиза.
_NEW_BOT_LIMIT = (
    r"(?:исчерпал\w*|достигнут\w*|превышен\w*)[^\n]{0,40}лимит|"
    r"лимит[^\n]{0,40}(?:исчерпан\w*|достигнут\w*|превышен\w*|попробуйте\s+позже)|"
    r"попробуйте\s+позже[^\n]{0,40}лимит|^\W*лимит"
)

# @MessageAnonBot и @chatruIetkabot (одно семейство): поиск — /next, вне диалога /search
# отвечает «Сначала подключи собеседника. Нажми /next».
_MESSAGE_ANON: dict[str, str] = {
    "search_command": "/next",
    "partner_found_pattern": r"начинай\s+общение",
    # Текст ухода собеседника не наблюдался: берём стиль семейства. «Ты закончил(а)
    # диалог» — это наш стоп, он идёт в stop_ack_pattern.
    "partner_left_pattern": (
        r"^\W*собеседник\s+(?:покинул|вышел|вышла|отключил|закончил)\S*\s+"
        r"(?:диалог|чат|общение)"
    ),
    "stop_ack_pattern": r"^\W*ты\s+закончил(?:\(а\)|а)?\s+диалог\s+с\s+собеседником",
    "service_pattern": (
        r"^\W*(?:подсказка\s+от\s+бота|надоели\s+случайные\s+собеседники|"
        r"хочешь\s+продолжить\s+общение\?|включи\s+поиск\s+по\s+полу|"
        r"сначала\s+подключи\s+собеседника)"
    ),
}

# Перед поиском на части аккаунтов бот спрашивает «Уточним перед поиском...» с кнопками:
# это служебное сообщение, автоответа нет (разовая ручная настройка аккаунта).
_NEKTOME: dict[str, str] = {
    "search_command": "/search",
    "partner_found_pattern": r"комната\b[^\n]{0,40}собеседник\s+найден",
    "partner_left_pattern": r"^\W*собеседник\s+закончил\S*\s+диалог",
    "stop_ack_pattern": r"^\W*вы\s+закончили\s+диалог",
    "service_pattern": (
        r"^\W*(?:сейчас\s+нет\s+разговора|уточним\s+перед\s+поиском|"
        r"тебе\s+пришло\s+новое\s+сообщение|вместе\s+с\s+вами\s+общаются|"
        r"выберите\s+действие)|"
        r"^[^\w\s]\W*[^\n]{0,40}?ждё?т\s+твоего\s+ответа"
    ),
}

# Уход собеседника и наш стоп бот подтверждает одним текстом, поэтому stop_ack не нужен.
# Новый аккаунт должен один раз вручную пройти регистрацию («Какой ваш пол?»).
_SECRETME: dict[str, str] = {
    "search_command": "/search",
    "partner_found_pattern": r"^\W*диалог\s+начат\b",
    "partner_left_pattern": r"^\W*диалог\s+#\S+\s+заверш[её]н",
    "service_pattern": (
        r"^\W*(?:оцените\s+реакцией|команда\s+недоступна\s+вне\s+диалога|"
        r"какой\s+ваш\s+пол|отправьте\s+/search)"
    ),
}

DEFAULT_BOT_PROFILES: dict[str, BotProfile] = {
    normalize_bot(name): BotProfile(
        username=name, stop_command="/stop", limit_pattern=_NEW_BOT_LIMIT, **fields
    )
    for name, fields in (
        ("@MessageAnonBot", _MESSAGE_ANON),
        ("@nektome_mibot", _NEKTOME),
        ("@secretme", _SECRETME),
    )
}
# Не запасной бот: профиль нужен аккаунту spec1 (его anon_* остаются главнее).
# reply_timeout 0 — как было до профиля: детектор «нет ответа» для него выключен.
DEFAULT_BOT_PROFILES["chatruietkabot"] = BotProfile(
    username="@chatruIetkabot",
    stop_command="/stop",
    limit_pattern=_NEW_BOT_LIMIT,
    reply_timeout_seconds=0,
    **_MESSAGE_ANON,
)


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
    anon_stop_ack_pattern: str | None = Field(
        None, description="Подтверждение нашего стопа: не считается уходом собеседника"
    )
    anon_service_pattern: str | None = Field(
        None, description="Служебные/рекламные сообщения бота: не попадают в диалог"
    )
    anon_bots: list[str] | None = Field(
        None, description="Запасные боты по порядку; [] — без запасных, null — ANON_FALLBACK_BOTS"
    )
    anon_bot_profiles: dict[str, BotProfile] | None = Field(
        None, description="Профили ботов этого аккаунта (ключ — имя бота)"
    )

    @field_validator(
        "anon_partner_found_pattern",
        "anon_partner_left_pattern",
        "anon_limit_pattern",
        "anon_stop_ack_pattern",
        "anon_service_pattern",
    )
    @classmethod
    def _compilable(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _check_regex(value, f"Account {info.data.get('id', '?')!r}", info.field_name)


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
    anon_stop_ack_pattern: str | None = None
    anon_service_pattern: str | None = None
    # Основной бот первым, затем запасные по приоритету.
    bots: list[ResolvedBot] = []


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
    selftest_url: str | None = Field(
        None, description="Адрес API для `bot selftest`; по умолчанию http://127.0.0.1:{api_port}"
    )
    cors_origins: list[str] = ["*"]

    anon_bot_username: str = Field(
        "@AnonRuBot",
        description="Default anonymous chat bot for accounts that do not override it",
    )
    anon_search_command: str = "/search"
    anon_stop_command: str = "/stop"
    anon_partner_found_pattern: str = (
        r"^\W*(?:собеседник найден|найден собеседник|partner found|chat started)"
    )
    anon_partner_left_pattern: str = (
        r"закончил(?:а|и)? с вами связь|собеседник (покинул|вышел|отключ|закончил)|"
        r"диалог (завершен|завершён|окончен)|partner left|chat (ended|stopped)"
    )

    anon_stop_ack_pattern: str | None = r"^\W*вы закончили связь с вашим собеседником"
    anon_service_pattern: str | None = (
        r"^\W*(?:если хотите, )?оставьте мнение о вашем собеседнике|"
        r"^\W*для улучшения поиска, укажите ваш пол"
    )

    anon_limit_pattern: str = r"исчерпали дневной лимит|лимит на чаты будет снят через"
    anon_limit_default_seconds: int = Field(
        3600, ge=60, description="Limit duration when the bot text has no time to wait"
    )

    anon_fallback_bots: list[str] = Field(
        ["@MessageAnonBot", "@nektome_mibot", "@secretme"],
        description="Запасные боты, если основной недоступен (JSON в env)",
    )
    anon_bot_profiles: dict[str, BotProfile] = Field(
        {}, description="Профили ботов по имени (JSON в env)"
    )
    anon_bot_unavailable_seconds: int = Field(
        3600, ge=60, description="На сколько отключать бота, который недоступен или молчит"
    )
    anon_bot_reply_timeout_seconds: int = Field(
        45, ge=0, description="Ждать ответ бота на поиск, если профиль не задаёт своё значение"
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

    def _resolve_bots(self, config: AccountConfig, primary_name: str) -> list[ResolvedBot]:
        """Основной бот и запасные. Приоритет полей: профиль аккаунта, ANON_BOT_PROFILES,
        встроенный профиль, глобальные ANON_*. У основного бота anon_* аккаунта главнее всего."""
        fallback = self.anon_fallback_bots if config.anon_bots is None else config.anon_bots
        layers: list[dict[str, BotProfile]] = [
            {normalize_bot(k): v for k, v in (config.anon_bot_profiles or {}).items()},
            {normalize_bot(k): v for k, v in self.anon_bot_profiles.items()},
            DEFAULT_BOT_PROFILES,
        ]
        bots: list[ResolvedBot] = []
        seen: set[str] = set()
        for index, name in enumerate([primary_name, *fallback]):
            key = normalize_bot(name)
            if not key or key in seen:
                continue
            seen.add(key)
            profiles = [layer[key] for layer in layers if key in layer]
            own = config if index == 0 else None

            def pick(attr: str, global_value: Any, own=own, profiles=profiles) -> Any:
                if own is not None and getattr(own, f"anon_{attr}"):
                    return getattr(own, f"anon_{attr}")
                for profile in profiles:
                    if getattr(profile, attr):
                        return getattr(profile, attr)
                return global_value

            # Детектор «нет ответа» включается только для ботов с профилем.
            timeout = next(
                (p.reply_timeout_seconds for p in profiles if p.reply_timeout_seconds is not None),
                self.anon_bot_reply_timeout_seconds if profiles else 0,
            )
            bots.append(
                ResolvedBot(
                    username=name.strip(),
                    search_command=pick("search_command", self.anon_search_command),
                    stop_command=pick("stop_command", self.anon_stop_command),
                    partner_found_pattern=pick(
                        "partner_found_pattern", self.anon_partner_found_pattern
                    ),
                    partner_left_pattern=pick(
                        "partner_left_pattern", self.anon_partner_left_pattern
                    ),
                    stop_ack_pattern=pick(
                        "stop_ack_pattern", None if profiles else self.anon_stop_ack_pattern
                    ),
                    limit_pattern=pick("limit_pattern", self.anon_limit_pattern),
                    service_pattern=pick(
                        "service_pattern", None if profiles else self.anon_service_pattern
                    ),
                    reply_timeout_seconds=timeout,
                    profiled=bool(profiles),
                )
            )
        return bots

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

            primary = config.anon_bot_username or self.anon_bot_username
            session_path = config.session_name or str(self.sessions_dir / config.id)
            resolved.append(
                ResolvedAccount(
                    id=config.id,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_path=session_path,
                    phone=config.phone,
                    anon_bot_username=primary,
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
                    anon_stop_ack_pattern=config.anon_stop_ack_pattern,
                    anon_service_pattern=config.anon_service_pattern,
                    bots=self._resolve_bots(config, primary),
                )
            )

        if not resolved:
            raise ValueError("No accounts configured")
        return resolved


settings = Settings()
