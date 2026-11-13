import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.services.anon_chat import AnonChatService, ChatState, Direction

FOUND = (
    "💭 Начинай общение!\n\n/stop — закончить диалог\n/next — найти следующего\n\n"
    "Комната: Пошлый чат 😈"
)
LEFT = (
    "💬 Ты закончил(а) диалог с собеседником\n\n/next — найти следующего\n"
    "/report — пожаловаться на спам"
)
PARTNER_LEFT = "💬 Собеседник закончил диалог\n\n/next — найти следующего"
SERVICE = [
    "💡 Подсказка от бота: используй /next",
    "🔍 Надоели случайные собеседники? Включи поиск по полу",
    "Сначала подключи собеседника. Нажми /next",
    "💫 Хочешь продолжить общение?\n\nВерни собеседника в диалог 👇",
]


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, _peer: str, text: str) -> None:
        self.sent.append(text)


def example_spec1() -> dict:
    root = Path(__file__).resolve().parent.parent
    items = json.loads((root / "accounts.example.json").read_text(encoding="utf-8"))
    return next(item for item in items if item["id"] == "spec1")


def make(items: list[dict]) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        settings = Settings(api_id=1, api_hash="h", accounts_file=path, _env_file=None)
        return settings, settings.resolve_accounts()


class OverrideResolutionTest(unittest.TestCase):
    def test_fallback_to_global(self) -> None:
        settings, (acc,) = make([{"id": "main"}])
        self.assertEqual(acc.anon_search_command, settings.anon_search_command)
        self.assertEqual(acc.anon_partner_found_pattern, settings.anon_partner_found_pattern)
        self.assertEqual(acc.anon_limit_pattern, settings.anon_limit_pattern)
        self.assertIsNone(acc.anon_service_pattern)

    def test_overrides_win(self) -> None:
        settings, (acc,) = make([example_spec1()])
        self.assertEqual(acc.anon_search_command, "/next")
        self.assertEqual(acc.anon_stop_command, "/stop")
        self.assertEqual(acc.anon_partner_found_pattern, "начинай общение")
        self.assertEqual(acc.anon_limit_pattern, settings.anon_limit_pattern)
        self.assertTrue(acc.anon_service_pattern)

class AdultBotTextsTest(unittest.IsolatedAsyncioTestCase):
    def service(self) -> tuple[AnonChatService, FakeClient]:
        settings, (acc,) = make([example_spec1()])
        client = FakeClient()
        svc = AnonChatService(client, settings, acc.anon_bot_username, account=acc)  # type: ignore[arg-type]
        return svc, client

    async def test_full_flow(self) -> None:
        svc, client = self.service()
        await svc.start_chat()
        self.assertEqual(client.sent, ["/next"])
        await svc.handle_bot_message(FOUND)
        self.assertIs(svc.state, ChatState.CHATTING)
        await svc.handle_bot_message("привет")
        self.assertEqual(svc.history()[-1].direction, Direction.INCOMING)
        await svc.handle_bot_message(PARTNER_LEFT)
        self.assertIs(svc.state, ChatState.IDLE)

    async def test_stop_ack_does_not_end_dialog(self) -> None:
        svc, _ = self.service()
        await svc.start_chat()
        await svc.handle_bot_message(FOUND)
        await svc.handle_bot_message(LEFT)
        self.assertIs(svc.state, ChatState.CHATTING)
        self.assertEqual(svc.history()[-1].direction, Direction.SYSTEM)

    async def test_service_messages_not_relayed(self) -> None:
        svc, _ = self.service()
        await svc.start_chat()
        await svc.handle_bot_message(FOUND)
        before = svc.last_seq
        for text in SERVICE:
            await svc.handle_bot_message(text)
        self.assertEqual(svc.last_seq, before)
        self.assertIs(svc.state, ChatState.CHATTING)

    async def test_found_left_win_over_service_phrase(self) -> None:
        svc, _ = self.service()
        await svc.start_chat()
        await svc.handle_bot_message(FOUND + "\n\n💡 Подсказка от бота")
        self.assertIs(svc.state, ChatState.CHATTING)
        await svc.handle_bot_message(PARTNER_LEFT + "\n\nХочешь продолжить общение?")
        self.assertIs(svc.state, ChatState.IDLE)

    async def test_partner_text_with_tip_phrase_is_kept(self) -> None:
        svc, _ = self.service()
        await svc.start_chat()
        await svc.handle_bot_message(FOUND)
        await svc.handle_bot_message("хочешь продолжить общение в тг?")
        last = svc.history()[-1]
        self.assertEqual(last.direction, Direction.INCOMING)
        self.assertEqual(last.text, "хочешь продолжить общение в тг?")

    def test_bad_pattern_fails_at_resolve(self) -> None:
        with self.assertRaisesRegex(ValueError, "bad"):
            make([{"id": "x", "anon_service_pattern": "(unclosed"}])

    async def test_default_service_has_no_filter(self) -> None:
        settings = Settings(_env_file=None)
        svc = AnonChatService(FakeClient(), settings, "@bot")  # type: ignore[arg-type]
        await svc.handle_bot_message(SERVICE[0])
        self.assertEqual(svc.last_seq, 1)
