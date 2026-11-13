import unittest
from types import SimpleNamespace

from app.config import AccountConfig, Settings
from app.handlers.anon_chat import press_button_factory
from app.services.anon_chat import AnonChatService, ChatState

A, B = "@MessageAnonBot", "@nektome_mibot"
ROOM = "💭 Ты сейчас в комнате «Обычный чат»\n\nЗдесь просто общаются.\n\nИщешь вирт?"
NEKTOME = "Уточним перед поиском. Что тебе сейчас ближе?"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, peer: str, text: str) -> None:
        self.sent.append((peer, text))


class Clicker:
    """click-колбэк: кнопки заданы текстами; ищет по regex, как настоящий."""

    def __init__(self, buttons: list[str], error: Exception | None = None) -> None:
        self.buttons = buttons
        self.error = error
        self.pressed: list[str] = []

    async def __call__(self, pattern) -> bool:
        if self.error:
            raise self.error
        for text in self.buttons:
            if pattern.search(text):
                self.pressed.append(text)
                return True
        return False


def make(first: str = A, second: str = B):
    settings = Settings(_env_file=None, anon_fallback_bots=[second])
    bots = settings._resolve_bots(AccountConfig(id="acc"), first)
    account = SimpleNamespace(id="acc", bots=bots)
    client = FakeClient()
    return AnonChatService(client, settings, first, account=account), client  # type: ignore[arg-type]


class SearchPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_profile_defaults(self) -> None:
        service, _ = make()
        a, b = service._bots
        self.assertIsNotNone(a.prompt_re)
        self.assertTrue(a.prompt_button_re.search("✅ Да, искать здесь"))
        self.assertFalse(a.prompt_button_re.search("💭 Обычный чат"))
        self.assertTrue(b.prompt_button_re.search("Пропустить"))
        self.assertFalse(b.prompt_button_re.search("Пропустить ход"))

    async def test_button_pressed_state_stays(self) -> None:
        service, client = make()
        await service.start_chat()
        click = Clicker(["✅ Да, искать здесь", "💭 Обычный чат", "🔕 Больше не спрашивать"])
        before = service.last_seq
        await service.handle_bot_message(ROOM, A, click)
        self.assertEqual(click.pressed, ["✅ Да, искать здесь"])
        self.assertEqual(service.state, ChatState.SEARCHING)
        self.assertEqual(service.last_seq, before)
        self.assertEqual(client.sent, [(A, "/next")])

    async def test_no_button_falls_back(self) -> None:
        service, client = make()
        await service.start_chat()
        await service.handle_bot_message(ROOM, A, Clicker(["💭 Обычный чат"]))
        self.assertEqual(service.state, ChatState.SEARCHING)
        self.assertEqual(service.bot_username, B)
        self.assertEqual(client.sent[-1], (B, "/search"))

    async def test_no_click_callable_falls_back(self) -> None:
        service, _ = make()
        await service.start_chat()
        await service.handle_bot_message(ROOM, A)
        self.assertEqual(service.bot_username, B)

    async def test_click_error_falls_back(self) -> None:
        service, client = make()
        await service.start_chat()
        await service.handle_bot_message(ROOM, A, Clicker([], error=RuntimeError("boom")))
        self.assertEqual(service.bot_username, B)
        self.assertEqual(client.sent[-1], (B, "/search"))

    async def test_nektome_skip_exact(self) -> None:
        service, _ = make(B, A)
        await service.start_chat()
        click = Clicker(["💘 Серьёзное знакомство", "Пропустить ход", "Пропустить"])
        await service.handle_bot_message(NEKTOME, B, click)
        self.assertEqual(click.pressed, ["Пропустить"])
        self.assertEqual(service.bot_username, B)

    async def test_press_limit_per_search(self) -> None:
        service, _ = make()
        await service.start_chat()
        click = Clicker(["Да, искать здесь"])
        for _ in range(3):
            await service.handle_bot_message(ROOM, A, click)
        self.assertEqual(len(click.pressed), 2)
        self.assertEqual(service.bot_username, B)

    async def test_partner_text_while_chatting_not_treated(self) -> None:
        service, _ = make()
        await service.start_chat()
        await service.handle_bot_message("Начинай общение", A)
        click = Clicker(["Да, искать здесь"])
        await service.handle_bot_message("ты сейчас в комнате?", A, click)
        self.assertEqual(click.pressed, [])
        self.assertEqual(service.state, ChatState.CHATTING)
        self.assertEqual(service.history()[-1].text, "ты сейчас в комнате?")

    async def test_idle_prompt_ignored(self) -> None:
        service, _ = make()
        click = Clicker(["Да, искать здесь"])
        await service.handle_bot_message(ROOM, A, click)
        self.assertEqual(click.pressed, [])
        self.assertEqual(service.history(), [])

    async def test_probe_presses_button(self) -> None:
        service, _ = make()
        queue = await service.begin_probe()
        click = Clicker(["Да, искать здесь"])
        await service.handle_bot_message(ROOM, A, click)
        self.assertEqual(click.pressed, ["Да, искать здесь"])
        self.assertTrue(queue.empty())

    async def test_telethon_button_factory(self) -> None:
        clicked: list[str] = []

        class Btn:
            def __init__(self, text: str) -> None:
                self.text = text

            async def click(self) -> None:
                clicked.append(self.text)

        service, _ = make()
        pattern = service._bots[0].prompt_button_re
        message = SimpleNamespace(buttons=[[Btn("A"), Btn("✅ Да, искать здесь")]])
        self.assertTrue(await press_button_factory(message)(pattern))
        self.assertEqual(clicked, ["✅ Да, искать здесь"])
        self.assertFalse(await press_button_factory(SimpleNamespace(buttons=None))(pattern))


if __name__ == "__main__":
    unittest.main()
