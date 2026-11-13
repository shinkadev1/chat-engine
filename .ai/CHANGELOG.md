# История важных изменений

## 2026-10-31 — оркестрация и фиксация контекста

Добавлены .codex/config.toml и explorer/planner/developer/reviewer/reporter, правила routing и workflow в AGENTS.md. Модель planner/reviewer — gpt-6-astra/high по подтверждённому выбору пользователя. Обновлены правила и память; контекст и конфигурация сохраняются отдельным локальным коммитом без прикладных изменений и push. Проверки: 42 TOML, строгая загрузка конфигураций в 7 корнях, diff check, reviewer PASS; mobile typecheck прошёл. Запуск всех ролей и ESLint не проверены (ESLint не настроен).

## 2026-10-31 — локальный контекст репозитория

Добавлены PROJECT, ARCHITECTURE, CONVENTIONS, CURRENT_TASK, STATE и CHANGELOG в .ai/; AGENTS.md направляет к ним будущие сессии. Комплект описывает именно chat-engine и не требует соседних репозиториев для начала работы. Существующие инструкции сохранены.

Код и деплой не менялись. Проверки контекста и ограничения отражены в [STATE.md](STATE.md).

Историческая точка исходников: `main` / `74485cf`. Это не подтверждение деплоя или отсутствия незакоммиченных изменений.

## 2026-11-11 — состояние limited (не выкачено)

При дневном лимите анонимного бота аккаунт переходит в limited с таймером; поле limited_until в /accounts и chat status; start/stop в limited отклоняются. Добавлены pytest-тесты (tests/test_limit.py) и шаг Tests в CI. Задача 2026-11-11-engine-limit-failover; до релиза не выкачено.
## Формат следующих записей

Дата, изменение и причина, затронутые контракты, коммит/файлы и реальные проверки. Текущие планы хранить в CURRENT_TASK; актуальные ограничения — в STATE.

## 2026-11-12 — режим аккаунта mode (не выкачено)

Контракт: в accounts.json необязательное `mode` ("normal"|"adult", по умолчанию normal; иное — ошибка при старте); `GET /api/v1/accounts` возвращает `mode`. Пример spec1 в accounts.example.json. Тесты: tests/test_accounts_mode.py.

## 2026-11-12 — per-account anon_* (не выкачено)

Контракт accounts.json: необязательные anon_search_command/stop_command/partner_found_pattern/partner_left_pattern/limit_pattern/service_pattern. Совпавшее с service-паттерном сообщение отбрасывается до классификации. Тесты: tests/test_account_overrides.py.

## 2026-11-13 — несколько ботов на аккаунт (не выкачено)

Контракт: ResolvedAccount.bots, accounts.json `anon_bots`/`anon_bot_profiles`, env ANON_FALLBACK_BOTS/ANON_BOT_PROFILES/ANON_BOT_UNAVAILABLE_SECONDS/ANON_BOT_REPLY_TIMEOUT_SECONDS. `GET /accounts` получил `bots[]` (аддитивно). Обработчик без from_users, фильтр по username. CLI `dump-bot`. Тесты: tests/test_multi_bot.py, tests/test_bot_config.py.

## 2026-11-13 — selftest (не выкачено)

Контракт: `POST /api/v1/selftest` (Bearer; 404 неизвестный аккаунт, 409 прогон уже идёт), env SELFTEST_URL для CLI. CLI `bot selftest [--account] [--bot] [--dry-run] [--text] [--*-timeout]`. Режим probe в AnonChatService: сообщения ботов идут только в очередь проверки, не в историю и события. Тесты: tests/test_selftest.py.

## 2026-11-13 — профили ботов по реальным текстам (не выкачено)

Встроенные профили @MessageAnonBot/@chatruIetkabot (поиск `/next`), @nektome_mibot, @secretme переписаны по текстам с прода; добавлено `stop_ack_pattern` (профиль) / `anon_stop_ack_pattern` (аккаунт, ENV-глобал) и глобальные service/stop_ack для @AnonRuBot. Ack нашего стопа в проде не меняет состояние, в selftest `closed` = left или ack (служебное само не закрывает). Тесты: tests/test_real_bot_texts.py.
