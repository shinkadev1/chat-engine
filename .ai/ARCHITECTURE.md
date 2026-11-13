# Архитектура chat-engine

Проверено: **2026-10-31**.

## Поток данных

engine_bridge → REST/WS `/api/v1/accounts/{id}/chat/...` → AnonChatService → Telethon → внешний анонимный бот. Ответы классифицируются как служебные события или сообщения и публикуются подписчикам.

## Точки входа

| Путь | Ответственность |
| --- | --- |
| `src/app/cli.py` | Команды serve / login / accounts. |
| `src/app/accounts.py` | Менеджер аккаунтов и отдельных Telethon-клиентов. |
| `src/app/client.py` | Создание TelegramClient. |
| `src/app/config.py` | Настройки аккаунтов и внешнего бота. |
| `src/app/services/anon_chat.py` | Фазы диалога, история и события. |
| `src/app/handlers/anon_chat.py` | Распознавание служебных сообщений внешнего бота. |
| `src/app/api/app.py` | FastAPI и lifespan. |
| `src/app/api/routers/` | REST управления аккаунтами/чатами и WebSocket. |
| `src/app/static/index.html` | Веб-интерфейс управления. |
| `pyproject.toml` | Зависимости, CLI и Ruff. |

## Контракты и ограничения

- У каждого аккаунта отдельные клиент и диалог; один активный диалог на аккаунт.
- Фазы: idle → searching → chatting; остановка или выход собеседника возвращают idle. Состояние limited: бот ответил про дневной лимит (ANON_LIMIT_PATTERN, не в chatting); срок берётся из «через ЧЧ:ММ:СС» или ANON_LIMIT_DEFAULT_SECONDS (3600), таймер возвращает idle; start/stop в limited дают 409, /stop боту не шлётся. Срок отдаётся в limited_until у AccountOut и ChatStatus.
- История и состояние хранятся в памяти; запуск нескольких реплик без внешнего состояния не является безопасным масштабированием.
- При заданном API_TOKEN маршруты `/api/v1` защищены Bearer-токеном; браузерный WS поддерживает подпротоколы bearer/token.
- Telegram-сессии создаются отдельным интерактивным login и должны переживать перезапуск контейнера.

## Развёртывание

`.github/workflows/ci-cd.yml`: lint, сборка и деплой из `main`. `deploy/deploy.sh` проверяет healthcheck и предусматривает откат образа.

Схема описывает репозиторий, а не подтверждённое состояние production.

## Несколько ботов (2026-11-13)

`config.py` собирает `ResolvedAccount.bots`; `services/anon_chat.py` ведёт per-bot `until`/`reason` и выбирает бота при поиске; `handlers/anon_chat.py` ловит все личные сообщения и отбирает по username через `service.match_bot`; `cli.py dump-bot` читает историю чата с ботом.
