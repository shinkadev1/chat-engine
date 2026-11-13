"""Реальные тексты анонимных ботов (сняты с прод-аккаунтов) должны классифицироваться верно."""

import re
import unittest

from app.config import AccountConfig, Settings

# Порядок проверки как в AnonChatService.handle_bot_message: limit, found, stop_ack, left, service.
ORDER = ("limit", "found", "stop_ack", "left", "service")

PARTNER_TEXTS = [
    "привет",
    "как дела?",
    "ты где нашёл этот чат",
    "диалог интересный",
    "я закончил диалог с работой",
    "хочешь продолжить общение в тг?",
    "а потом собеседник найден",
    "ты закончил диалог?",
    "он ждёт твоего ответа",
    "если хочешь, оставь мнение: оставьте мнение о вашем собеседнике",
    "ну вы закончили связь с вашим собеседником?",
]

CASES: dict[str, list[tuple[str, str]]] = {
    "@AnonRuBot": [
        ("found", "Собеседник найден\n\n/next - искать нового собеседника\n/stop - закончить"),
        ("left", "Собеседник закончил с вами связь 😞\n\nНапишите /search для поиска"),
        ("stop_ack", "Вы закончили связь с вашим собеседником 🙄\n\nНапишите /search"),
        ("service", "Если хотите, оставьте мнение о вашем собеседнике..."),
        ("service", "Для улучшения поиска, укажите ваш пол"),
    ],
    "@MessageAnonBot": [
        (
            "found",
            "💭 Начинай общение!\n\n/stop — закончить диалог\n/next — найти следующего\n\n"
            "Комната: Обычный чат 💭",
        ),
        (
            "stop_ack",
            "💬 Ты закончил(а) диалог с собеседником\n\n/next — найти следующего\n"
            "/report — пожаловаться на спам",
        ),
        ("service", "💫 Хочешь продолжить общение?\n\nВерни собеседника в диалог 👇"),
        ("service", "💡 Подсказка от бота\n\nпосоветую"),
        ("service", "🔍 Надоели случайные собеседники?\n\nВключи поиск по полу..."),
        ("service", "Сначала подключи собеседника. Нажми /next"),
        ("left", "💬 Собеседник закончил диалог"),
    ],
    "@chatruIetkabot": [
        ("found", "💭 Начинай общение!\n\n/stop — закончить диалог\n\nКомната: Пошлый чат 😈"),
        ("stop_ack", "💬 Ты закончил(а) диалог с собеседником\n\n/next — найти следующего"),
        ("service", "Сначала подключи собеседника. Нажми /next"),
    ],
    "@nektome_mibot": [
        ("found", "Комната 💬 Общение, собеседник найден. Общайтесь!\n\n/stop"),
        ("found", "✅ Комната 💬 Общение, собеседник найден. Общайтесь!"),
        ("left", "Собеседник закончил диалог 😞\n\nПожалуйста, нажмите Пожаловаться..."),
        ("stop_ack", "🛑 Вы закончили диалог\n\nПожалуйста, нажмите Пожаловаться..."),
        ("service", "Сейчас нет разговора. Найти собеседника — /search"),
        ("service", "Уточним перед поиском. Что тебе сейчас ближе?"),
        ("service", "Тебе пришло новое сообщение"),
        ("service", "⏳ Кто-то ждёт твоего ответа"),
        ("service", "😈 Вместе с вами общаются 5 человек(а)\n\n⚡️ Выберите действие:"),
    ],
    "@secretme": [
        ("found", "💭 Диалог начат\n\nПол: **\nВозраст: **"),
        ("left", "💭 Диалог #113.517.011 завершен\n\n/sendlink ..."),
        ("service", "Оцените реакцией своего собеседника"),
        ("service", "🚫 Команда недоступна вне диалога\n\n/search - искать собеседника"),
        ("service", "🚻 Какой ваш пол?"),
        ("service", "💭 Отправьте /search для поиска собеседника"),
    ],
}


def classify(bot, text: str) -> str:
    patterns = {
        "limit": bot.limit_pattern,
        "found": bot.partner_found_pattern,
        "stop_ack": bot.stop_ack_pattern,
        "left": bot.partner_left_pattern,
        "service": bot.service_pattern,
    }
    for kind in ORDER:
        pattern = patterns[kind]
        if pattern and re.search(pattern, text, re.IGNORECASE):
            return kind
    return "none"


class RealBotTextsTest(unittest.TestCase):
    def resolve(self, name: str):
        settings = Settings(_env_file=None)
        config = AccountConfig(id="x", anon_bot_username=name, anon_bots=[])
        (bot,) = settings._resolve_bots(config, name)
        return bot

    def test_search_commands(self) -> None:
        for name, command in (
            ("@AnonRuBot", "/search"),
            ("@MessageAnonBot", "/next"),
            ("@chatruIetkabot", "/next"),
            ("@nektome_mibot", "/search"),
            ("@secretme", "/search"),
        ):
            self.assertEqual(self.resolve(name).search_command, command, name)

    def test_real_texts(self) -> None:
        for name, cases in CASES.items():
            bot = self.resolve(name)
            for expected, text in cases:
                with self.subTest(bot=name, text=text[:40]):
                    self.assertEqual(classify(bot, text), expected)

    def test_secretme_stop_ack_is_left(self) -> None:
        bot = self.resolve("@secretme")
        self.assertIsNone(bot.stop_ack_pattern)

    def test_partner_messages_are_not_service(self) -> None:
        for name in CASES:
            bot = self.resolve(name)
            for text in PARTNER_TEXTS:
                with self.subTest(bot=name, text=text):
                    self.assertEqual(classify(bot, text), "none")

    def test_account_override_wins_for_stop_ack(self) -> None:
        settings = Settings(_env_file=None)
        config = AccountConfig(
            id="x", anon_bot_username="@MessageAnonBot", anon_stop_ack_pattern="custom"
        )
        (bot,) = settings._resolve_bots(
            config.model_copy(update={"anon_bots": []}), "@MessageAnonBot"
        )
        self.assertEqual(bot.stop_ack_pattern, "custom")
