import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.services.anon_chat import (
    AnonChatService,
    ChatError,
    ChatState,
    EventType,
    parse_limit_duration,
    partner_text,
)

LIMIT_TEXT = (
    "Похоже, вы исчерпали дневной лимит чатов.\n"
    "Вы можете продолжить общение завтра или оформить подписку.\n"
    "Лимит на чаты будет снят через 03:46:19"
)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, _peer: str, text: str) -> None:
        self.sent.append(text)


def make_service() -> tuple[AnonChatService, FakeClient]:
    client = FakeClient()
    service = AnonChatService(client, Settings(_env_file=None), "@bot")  # type: ignore[arg-type]
    return service, client


def drain(queue: asyncio.Queue) -> list:
    return [queue.get_nowait() for _ in range(queue.qsize())]


class LimitTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_duration(self) -> None:
        self.assertEqual(
            parse_limit_duration("снят через 03:46:19"),
            timedelta(hours=3, minutes=46, seconds=19),
        )
        self.assertIsNone(parse_limit_duration("снят когда-нибудь"))
        self.assertIsNone(parse_limit_duration("через 03:99:19"))

    def test_limit_text_survives_partner_text(self) -> None:
        self.assertEqual(partner_text(LIMIT_TEXT), LIMIT_TEXT)

    async def test_limit_while_searching(self) -> None:
        service, _ = make_service()
        await service.start_chat()
        with service.subscribe() as queue:
            before = datetime.now(UTC)
            await service.handle_bot_message(LIMIT_TEXT)
            self.assertIs(service.state, ChatState.LIMITED)
            expected = before + timedelta(hours=3, minutes=46, seconds=19)
            assert service.limited_until is not None
            self.assertLess(abs(service.limited_until - expected), timedelta(seconds=5))
            events = drain(queue)
            self.assertTrue(
                any(e.type is EventType.STATE and e.state is ChatState.LIMITED for e in events)
            )
        await service.shutdown()

    async def test_start_and_stop_rejected_when_limited(self) -> None:
        service, client = make_service()
        await service.handle_bot_message(LIMIT_TEXT)
        with self.assertRaises(ChatError):
            await service.start_chat()
        with self.assertRaises(ChatError):
            await service.stop_chat()
        self.assertEqual(client.sent, [])
        await service.shutdown()

    async def test_limit_expires(self) -> None:
        service, _ = make_service()
        with service.subscribe() as queue:
            await service.handle_bot_message("Лимит на чаты будет снят через 00:00:00")
            await asyncio.sleep(0.05)
            self.assertIs(service.state, ChatState.IDLE)
            self.assertIsNone(service.limited_until)
            states = [e.state for e in drain(queue) if e.type is EventType.STATE]
            self.assertEqual(states[-1], ChatState.IDLE)

    async def test_chatting_text_is_incoming(self) -> None:
        service, _ = make_service()
        await service.handle_bot_message("chat started")
        self.assertIs(service.state, ChatState.CHATTING)
        await service.handle_bot_message("вы исчерпали дневной лимит")
        self.assertIs(service.state, ChatState.CHATTING)
        self.assertEqual(service.history()[-1].direction.value, "in")

    async def test_default_duration(self) -> None:
        service, _ = make_service()
        before = datetime.now(UTC)
        await service.handle_bot_message("Вы исчерпали дневной лимит")
        assert service.limited_until is not None
        delta = service.limited_until - before
        self.assertLess(abs(delta - timedelta(seconds=3600)), timedelta(seconds=5))
        await service.shutdown()

    async def test_shutdown_cancels_timer_without_stop(self) -> None:
        service, client = make_service()
        await service.handle_bot_message(LIMIT_TEXT)
        task = service._limit_task
        await service.shutdown()
        await asyncio.sleep(0)
        assert task is not None
        self.assertTrue(task.cancelled())
        self.assertEqual(client.sent, [])
