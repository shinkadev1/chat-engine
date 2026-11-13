import asyncio
import re
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telethon.errors import FloodWaitError

from app.api.routers.accounts import list_accounts
from app.config import _NEW_BOT_LIMIT, AccountConfig, Settings
from app.handlers.anon_chat import _bot_of
from app.services.anon_chat import AnonChatService, ChatState, Direction

from test_multi_bot import A, B, C, make


class PatternTests(unittest.TestCase):
    def test_limit_is_not_triggered_by_chat_phrases(self) -> None:
        rx = re.compile(_NEW_BOT_LIMIT, re.IGNORECASE)
        self.assertIsNone(rx.search("если долго, попробуйте позже"))
        self.assertIsNone(rx.search("привет, как дела? бывает лимит на всё"[:12]))
        self.assertTrue(rx.search("Вы исчерпали дневной лимит"))
        self.assertTrue(rx.search("Лимит исчерпан, попробуйте позже"))

    def test_profiled_flag_resolved(self) -> None:
        settings = Settings(_env_file=None)
        config = AccountConfig(id="x", anon_bot_username="@AnonRuBot")
        bots = {b.username: b for b in settings._resolve_bots(config, "@AnonRuBot")}
        self.assertFalse(bots["@AnonRuBot"].profiled)
        self.assertTrue(bots["@MessageAnonBot"].profiled)


class ServiceFixesTest(unittest.IsolatedAsyncioTestCase):
    async def test_strict_bot_ignores_found_text_in_chatting(self) -> None:
        svc, _ = make()
        svc._bots[0].strict = True
        await svc.start_chat()
        await svc.handle_bot_message("chat started", A)
        self.assertIs(svc.state, ChatState.CHATTING)
        await svc.handle_bot_message("chat started", A)
        self.assertIs(svc.state, ChatState.CHATTING)
        self.assertEqual(svc.history()[-1].direction, Direction.INCOMING)
        await svc.shutdown()

    async def test_non_strict_bot_keeps_old_behavior(self) -> None:
        svc, _ = make()
        await svc.start_chat()
        await svc.handle_bot_message("chat started", A)
        await svc.handle_bot_message("chat started", A)
        self.assertEqual(svc.history()[-1].direction, Direction.SYSTEM)
        await svc.shutdown()

    async def test_no_reply_sends_stop_to_first_bot(self) -> None:
        svc, client = make({A: 0.01})
        await svc.start_chat()
        await asyncio.sleep(0.1)
        self.assertEqual(client.sent, [(A, "/search"), (A, "/stop"), (B, "/search")])
        await svc.shutdown()

    async def test_connection_error_falls_through_to_next_bot(self) -> None:
        svc, client = make()
        await svc.start_chat()
        client.fail[B] = ConnectionError("down")
        await svc.handle_bot_message("исчерпали дневной лимит", A)
        self.assertIs(svc.state, ChatState.SEARCHING)
        self.assertEqual(svc.bot_username, C)
        status = {s.username: s for s in svc.bots_status()}
        self.assertEqual(status[B].reason, "send_error")
        await svc.shutdown()

    async def test_all_fail_enters_limited_with_timer(self) -> None:
        svc, client = make()
        await svc.start_chat()
        client.fail[B] = OSError("x")
        client.fail[C] = ConnectionError("y")
        await svc.handle_bot_message("исчерпали дневной лимит", A)
        self.assertIs(svc.state, ChatState.LIMITED)
        self.assertIsNotNone(svc._limit_task)
        await svc.shutdown()

    async def test_flood_wait_uses_exception_seconds(self) -> None:
        svc, client = make()
        client.fail[A] = FloodWaitError(request=None, capture=120)
        await svc.start_chat()
        status = {s.username: s for s in svc.bots_status()}
        self.assertEqual(status[A].reason, "flood_wait")
        assert status[A].until is not None
        left = status[A].until - datetime.now(UTC)
        self.assertLess(abs(left - timedelta(seconds=120)), timedelta(seconds=5))
        await svc.shutdown()


class BotOfTest(unittest.IsolatedAsyncioTestCase):
    async def test_requires_bot_flag(self) -> None:
        svc, _ = make()

        def event(sender):
            async def get_sender():
                return sender

            return SimpleNamespace(get_sender=get_sender)

        self.assertEqual(await _bot_of(event(SimpleNamespace(username="bot_a", bot=True)), svc), A)
        self.assertIsNone(await _bot_of(event(SimpleNamespace(username="bot_a", bot=False)), svc))
        self.assertIsNone(await _bot_of(event(SimpleNamespace(username="human", bot=False)), svc))


class AccountsBotsTest(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_returns_bots(self) -> None:
        svc, _ = make()
        account = SimpleNamespace(
            id="acc",
            authorized=False,
            username=None,
            config=SimpleNamespace(anon_bot_username=A, mode="normal"),
            service=svc,
        )
        out = await list_accounts(SimpleNamespace(list=lambda: [account]))
        self.assertEqual([b.username for b in out[0].bots], [A, B, C])
        self.assertTrue(all(b.available for b in out[0].bots))
        self.assertIsNone(out[0].state)


if __name__ == "__main__":
    unittest.main()
