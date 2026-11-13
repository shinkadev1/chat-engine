import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from telethon.errors import UserIsBlockedError

from app import cli
from app.config import Settings
from app.services.anon_chat import AnonChatService, ChatError, ChatState, Direction, EventType

LIMIT_TEXT = "Вы исчерпали дневной лимит.\nЛимит на чаты будет снят через 01:00:00"
A, B, C = "@bot_a", "@bot_b", "@bot_c"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}

    async def send_message(self, peer: str, text: str) -> None:
        if peer in self.fail:
            raise self.fail[peer]
        self.sent.append((peer, text))


def make(timeouts: dict[str, float] | None = None):
    timeouts = timeouts or {}
    bots = [
        SimpleNamespace(
            username=name,
            search_command="/search",
            stop_command="/stop",
            partner_found_pattern="chat started",
            partner_left_pattern="partner left",
            limit_pattern="исчерпали дневной лимит",
            service_pattern=None,
            reply_timeout_seconds=timeouts.get(name, 0),
        )
        for name in (A, B, C)
    ]
    account = SimpleNamespace(id="acc", bots=bots)
    client = FakeClient()
    service = AnonChatService(client, Settings(_env_file=None), A, account=account)  # type: ignore[arg-type]
    return service, client


def states(queue: asyncio.Queue) -> list[ChatState]:
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    return [e.state for e in events if e.type is EventType.STATE]


class MultiBotTest(unittest.IsolatedAsyncioTestCase):
    async def test_limit_while_searching_switches_bot(self) -> None:
        svc, client = make()
        await svc.start_chat()
        with svc.subscribe() as queue:
            await svc.handle_bot_message(LIMIT_TEXT, A)
            self.assertNotIn(ChatState.LIMITED, states(queue))
        self.assertIs(svc.state, ChatState.SEARCHING)
        self.assertEqual(client.sent, [(A, "/search"), (B, "/search")])
        status = {s.username: s for s in svc.bots_status()}
        self.assertFalse(status[A].available)
        self.assertEqual(status[A].reason, "limit")
        self.assertTrue(status[B].active)
        self.assertEqual(svc.bot_username, B)
        await svc.shutdown()

    async def test_all_unavailable_then_recovery(self) -> None:
        svc, client = make()
        await svc.start_chat()
        for _ in range(3):
            await svc.handle_bot_message(LIMIT_TEXT, svc.bot_username)
        self.assertIs(svc.state, ChatState.LIMITED)
        until = svc.limited_until
        assert until is not None
        expected = datetime.now(UTC) + timedelta(hours=1)
        self.assertLess(abs(until - expected), timedelta(seconds=5))
        with self.assertRaises(ChatError):
            await svc.start_chat()
        # Срок вышел: бот A снова свободен и снова приоритетный.
        for bot in svc._bots:
            bot.until = datetime.now(UTC) - timedelta(seconds=1)
        await svc._release_limit(0)
        self.assertIs(svc.state, ChatState.IDLE)
        client.sent.clear()
        await svc.start_chat()
        self.assertEqual(client.sent, [(A, "/search")])
        await svc.shutdown()

    async def test_send_error_moves_on(self) -> None:
        svc, client = make()
        client.fail[A] = ValueError("Cannot find any entity")
        client.fail[B] = UserIsBlockedError(request=None)
        await svc.start_chat()
        self.assertIs(svc.state, ChatState.SEARCHING)
        self.assertEqual(client.sent, [(C, "/search")])
        status = {s.username: s for s in svc.bots_status()}
        self.assertEqual(status[A].reason, "send_error")
        self.assertEqual(status[B].reason, "send_error")
        await svc.shutdown()

    async def test_all_send_errors_limit(self) -> None:
        svc, client = make()
        for name in (A, B, C):
            client.fail[name] = ValueError("nope")
        with self.assertRaises(ChatError):
            await svc.start_chat()
        self.assertIs(svc.state, ChatState.LIMITED)
        await svc.shutdown()

    async def test_no_reply_timeout(self) -> None:
        svc, client = make(timeouts={A: 0.05})
        await svc.start_chat()
        await asyncio.sleep(0.2)
        self.assertEqual(client.sent, [(A, "/search"), (A, "/stop"), (B, "/search")])
        status = {s.username: s for s in svc.bots_status()}
        self.assertEqual(status[A].reason, "no_reply")
        self.assertIs(svc.state, ChatState.SEARCHING)
        await svc.shutdown()

    async def test_reply_cancels_timeout(self) -> None:
        svc, client = make(timeouts={A: 0.05})
        await svc.start_chat()
        await svc.handle_bot_message("Ищем собеседника...", A)
        await asyncio.sleep(0.15)
        self.assertEqual(client.sent, [(A, "/search")])
        await svc.shutdown()

    async def test_inactive_bot_text_ignored_but_limit_counts(self) -> None:
        svc, _ = make()
        await svc.start_chat()
        before = svc.last_seq
        await svc.handle_bot_message("chat started", B)
        await svc.handle_bot_message("привет", B)
        self.assertEqual(svc.last_seq, before)
        self.assertIs(svc.state, ChatState.SEARCHING)
        await svc.handle_bot_message(LIMIT_TEXT, B)
        self.assertEqual(svc.last_seq, before)
        status = {s.username: s for s in svc.bots_status()}
        self.assertEqual(status[B].reason, "limit")
        await svc.shutdown()

    async def test_chatting_not_interrupted(self) -> None:
        svc, client = make()
        await svc.start_chat()
        await svc.handle_bot_message("chat started", A)
        self.assertIs(svc.state, ChatState.CHATTING)
        await svc.handle_bot_message(LIMIT_TEXT, A)
        await svc.handle_bot_message(LIMIT_TEXT, B)
        await svc.handle_bot_message("chat started", C)
        self.assertIs(svc.state, ChatState.CHATTING)
        self.assertEqual(svc.history()[-1].direction, Direction.INCOMING)
        await svc.send_message("hi")
        await svc.stop_chat()
        self.assertEqual(client.sent[-2:], [(A, "hi"), (A, "/stop")])
        await svc.shutdown()

    async def test_unknown_bot_ignored(self) -> None:
        svc, _ = make()
        await svc.start_chat()
        before = svc.last_seq
        await svc.handle_bot_message("chat started", "@stranger")
        self.assertEqual(svc.last_seq, before)
        self.assertIsNone(svc.match_bot("stranger"))
        self.assertEqual(svc.match_bot("BOT_B"), B)
        await svc.shutdown()


class DumpBotTest(unittest.IsolatedAsyncioTestCase):
    async def test_dump_sends_nothing(self) -> None:
        sent: list[str] = []

        class Client:
            async def send_message(self, *args, **kwargs) -> None:
                sent.append("x")

            async def iter_messages(self, bot, limit):
                for i in range(2):
                    yield SimpleNamespace(
                        date=datetime(2026, 1, 1, tzinfo=UTC),
                        out=bool(i),
                        message=f"msg {i}\nline",
                        buttons=[[SimpleNamespace(text="Кнопка")]] if i == 0 else None,
                    )

        lines = await cli.dump_messages(Client(), "@bot", 20)
        self.assertEqual(len(lines), 2)
        self.assertIn("buttons=['Кнопка']", lines[1])
        self.assertEqual(sent, [])
