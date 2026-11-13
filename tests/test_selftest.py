import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import cli, selftest
from app.accounts import UnknownAccountError
from app.api import deps
from app.api.routers.selftest import selftest as selftest_endpoint
from app.api.schemas import SelftestRequest
from app.config import Settings
from app.handlers.anon_chat import _log_incoming
from app.services.anon_chat import AnonChatService, ChatError, ChatState

LIMIT_TEXT = "Вы исчерпали дневной лимит.\nЛимит на чаты будет снят через 01:00:00"
A, B = "@bot_a", "@bot_b"
FAST = {"search_timeout": 0.3, "reply_timeout": 0.3, "close_timeout": 0.2}


class FakeClient:
    """Отвечает как бот по сценарию: {(бот, команда или текст): [ответы]}."""

    def __init__(self, service_ref: list, script: dict | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.service_ref = service_ref
        self.script = script or {}
        self.fail: dict[str, Exception] = {}
        self.me = SimpleNamespace(username="user1", restricted=False, scam=False, fake=False)
        self.authorized = True
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):
        return self.me

    async def send_message(self, peer: str, text: str) -> None:
        if peer in self.fail:
            raise self.fail[peer]
        self.sent.append((peer, text))
        for reply in self.script.get((peer, text), []):
            sender, body = reply if isinstance(reply, tuple) else (peer, reply)
            asyncio.get_running_loop().call_soon(
                lambda s=sender, b=body: asyncio.ensure_future(
                    self.service_ref[0].handle_bot_message(b, s)
                )
            )


def make(script: dict | None = None):
    ref: list = []
    bots = [
        SimpleNamespace(
            username=name,
            search_command="/search",
            stop_command="/stop",
            partner_found_pattern="chat started",
            partner_left_pattern="partner left",
            limit_pattern="исчерпали дневной лимит",
            service_pattern="searching",
            stop_ack_pattern="you stopped",
            reply_timeout_seconds=0,
        )
        for name in (A, B)
    ]
    config = SimpleNamespace(id="acc", bots=bots)
    client = FakeClient(ref, script)
    service = AnonChatService(client, Settings(_env_file=None), A, account=config)  # type: ignore[arg-type]
    ref.append(service)
    account = SimpleNamespace(
        id="acc", client=client, service=service, authorized=True, username="user1", config=config
    )
    return account, service, client


def happy(bot: str) -> dict:
    return {
        (bot, "/search"): ["searching", "chat started"],
        (bot, "Привет"): ["здравствуй"],
        (bot, "/stop"): ["partner left"],
    }


def pairs(results) -> dict[str, selftest.PairResult]:
    return {p.bot: p for p in results[0].pairs}


class SelftestTest(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path(self) -> None:
        account, service, client = make({**happy(A), **happy(B)})
        results = await selftest.run_selftest([account], **FAST)
        for pair in results[0].pairs:
            self.assertEqual(pair.status, selftest.REPLY_RECEIVED)
            self.assertTrue(pair.closed)
        self.assertEqual(
            client.sent[:4], [(A, "/search"), (A, "Привет"), (A, "/stop"), (B, "/search")]
        )
        self.assertFalse(service.probing)
        self.assertEqual(results[0].username, "user1")

    async def test_probe_isolated(self) -> None:
        account, service, client = make()
        with service.subscribe() as events:
            queue = await service.begin_probe()
            assert queue is not None
            self.assertIsNone(await service.begin_probe())
            with self.assertRaises(ChatError):
                await service.start_chat()
            await service.handle_bot_message("chat started", A)
            await service.handle_bot_message("hello", A)
            self.assertEqual(service.last_seq, 0)
            self.assertEqual(service.history(), [])
            self.assertTrue(events.empty())
            self.assertIsNone(service._reply_task)
            self.assertEqual(queue.qsize(), 2)
            self.assertIs(service.state, ChatState.IDLE)
            service.end_probe()
        self.assertEqual(client.sent, [])
        await service.start_chat()
        self.assertIs(service.state, ChatState.SEARCHING)
        await service.shutdown()

    async def test_busy(self) -> None:
        account, service, client = make()
        await service.start_chat()
        client.sent.clear()
        results = await selftest.run_selftest([account], **FAST)
        self.assertEqual({p.status for p in results[0].pairs}, {selftest.BUSY})
        self.assertEqual(client.sent, [])
        self.assertIs(service.state, ChatState.SEARCHING)
        await service.shutdown()

    async def test_limit_during_test(self) -> None:
        script = {(A, "/search"): [LIMIT_TEXT], **happy(B)}
        account, service, _ = make(script)
        results = await selftest.run_selftest([account], **FAST)
        found = pairs(results)
        self.assertEqual(found[A].status, selftest.LIMITED)
        self.assertEqual(found[B].status, selftest.REPLY_RECEIVED)
        self.assertEqual(service.history(), [])
        await service.shutdown()

    async def test_already_blocked(self) -> None:
        account, service, client = make(happy(B))
        service._bots[0].until = service._now().replace(year=2999)
        results = await selftest.run_selftest([account], **FAST)
        self.assertEqual(pairs(results)[A].status, selftest.LIMITED)
        self.assertNotIn((A, "/search"), client.sent)
        await service.shutdown()

    async def test_search_timeout_and_no_response(self) -> None:
        account, service, client = make({(B, "/search"): ["Подбираем вам пару, ждите"]})
        results = await selftest.run_selftest([account], **FAST)
        found = pairs(results)
        self.assertEqual(found[A].status, selftest.NO_RESPONSE)
        self.assertEqual(found[B].status, selftest.BOT_RESPONDED)
        # Поиск был отправлен, значит стоп тоже ушёл.
        self.assertIn((A, "/stop"), client.sent)
        self.assertFalse(found[A].closed)

    async def test_no_reply_from_partner(self) -> None:
        script = {(A, "/search"): ["chat started"], (A, "/stop"): ["partner left"]}
        account, _, _ = make(script)
        results = await selftest.run_selftest([account], bots=[A], **FAST)
        self.assertEqual(results[0].pairs[0].status, selftest.PARTNER_FOUND)
        self.assertTrue(results[0].pairs[0].closed)

    async def test_exception_then_next_pair(self) -> None:
        account, service, client = make(happy(B))
        client.fail[A] = ValueError("boom")
        results = await selftest.run_selftest([account], **FAST)
        found = pairs(results)
        self.assertEqual(found[A].status, selftest.ERROR)
        self.assertEqual(found[A].detail, "ValueError")
        self.assertIsNone(found[A].closed)
        self.assertEqual(found[B].status, selftest.REPLY_RECEIVED)
        self.assertFalse(service.probing)

    async def test_dry_run_and_flags(self) -> None:
        account, service, client = make(happy(A))
        results = await selftest.run_selftest([account], dry_run=True, **FAST)
        self.assertEqual({p.status for p in results[0].pairs}, {selftest.SKIPPED})
        self.assertEqual(client.sent, [])

        client.me = SimpleNamespace(username="u", restricted=True, scam=False, fake=False)
        results = await selftest.run_selftest([account], **FAST)
        self.assertEqual(results[0].flags, ["restricted"])
        self.assertEqual({p.status for p in results[0].pairs}, {selftest.SESSION})

        client.me = SimpleNamespace(username="u")
        client.authorized = False
        results = await selftest.run_selftest([account], **FAST)
        self.assertEqual(results[0].session, "not authorized")
        self.assertEqual(client.sent, [])

    async def test_filters(self) -> None:
        a1, _, c1 = make(happy(B))
        a2, _, c2 = make(happy(B))
        a2.id = "other"
        results = await selftest.run_selftest([a1, a2], bots=["BOT_B"], **FAST)
        self.assertEqual([len(r.pairs) for r in results], [1, 1])
        self.assertEqual(results[0].pairs[0].bot, B)
        self.assertTrue(all(peer == B for peer, _ in c1.sent + c2.sent))

    async def test_foreign_bot_not_counted(self) -> None:
        # Ответ пришёл от другого бота аккаунта: поиск A остаётся без ответа.
        account, _, _ = make({(A, "/search"): [(B, "chat started")]})
        results = await selftest.run_selftest([account], bots=[A], **FAST)
        self.assertEqual(results[0].pairs[0].status, selftest.NO_RESPONSE)

    async def test_probe_waits_for_start_chat(self) -> None:
        account, service, client = make()
        gate = asyncio.Event()
        original = client.send_message

        async def slow(peer: str, text: str) -> None:
            await gate.wait()
            await original(peer, text)

        client.send_message = slow  # type: ignore[method-assign]
        start = asyncio.create_task(service.start_chat())
        await asyncio.sleep(0.05)
        run = asyncio.create_task(selftest.run_selftest([account], **FAST))
        await asyncio.sleep(0.05)
        self.assertFalse(run.done() and service.probing)
        gate.set()
        await start
        results = await run
        self.assertEqual({p.status for p in results[0].pairs}, {selftest.BUSY})
        self.assertEqual(client.sent, [(A, "/search")])
        await service.shutdown()

    async def test_late_partner_text_not_closed(self) -> None:
        script = {**happy(A), (A, "/stop"): [("@bot_a", "поздно")]}
        account, service, _ = make(script)
        results = await selftest.run_selftest([account], bots=[A], **FAST)
        self.assertFalse(results[0].pairs[0].closed)
        self.assertFalse(service.probing)

    async def test_stop_ack_marks_closed(self) -> None:
        script = {**happy(A), (A, "/stop"): ["you stopped the chat"]}
        account, _, _ = make(script)
        results = await selftest.run_selftest([account], bots=[A], **FAST)
        self.assertTrue(results[0].pairs[0].closed)

    async def test_service_alone_does_not_close(self) -> None:
        script = {**happy(A), (A, "/stop"): ["searching ads"]}
        account, _, _ = make(script)
        results = await selftest.run_selftest([account], bots=[A], **FAST)
        self.assertFalse(results[0].pairs[0].closed)

    async def test_late_found_after_stop_not_chatting(self) -> None:
        script = {**happy(A), (A, "/stop"): ["chat started", "partner left"]}
        account, service, _ = make(script)
        results = await selftest.run_selftest([account], bots=[A], **FAST)
        self.assertTrue(results[0].pairs[0].closed)
        await asyncio.sleep(0.05)
        self.assertIsNot(service.state, ChatState.CHATTING)
        self.assertEqual(service.history(), [])

    async def test_text_hidden_when_probing(self) -> None:
        _, service, _ = make()
        message = SimpleNamespace(message="секрет", id=1, media=None, photo=None, reply_markup=None)
        await service.begin_probe()
        with self.assertLogs("app.handlers.anon_chat", "INFO") as logs:
            _log_incoming(service, message, "new", A)
        self.assertNotIn("секрет", logs.output[0])
        service.end_probe()


class RouterTest(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized(self) -> None:
        with patch.object(deps.settings, "api_token", "tok"):
            with self.assertRaises(HTTPException) as ctx:
                deps.require_token(None)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_running_gives_409(self) -> None:
        manager = SimpleNamespace(list=lambda: [])
        async with selftest._RUN_LOCK:
            with self.assertRaises(HTTPException) as ctx:
                await selftest_endpoint(SelftestRequest(), manager)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_unknown_account_404(self) -> None:
        def get(account_id: str):
            raise UnknownAccountError(account_id)

        manager = SimpleNamespace(get=get, list=lambda: [])
        with self.assertRaises(HTTPException) as ctx:
            await selftest_endpoint(SelftestRequest(accounts=["nope"]), manager)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_endpoint_dry_run(self) -> None:
        account, _, _ = make()
        manager = SimpleNamespace(list=lambda: [account])
        out = await selftest_endpoint(SelftestRequest(dry_run=True), manager)  # type: ignore[arg-type]
        self.assertTrue(out.dry_run)
        self.assertEqual(out.results[0].pairs[0].status, selftest.SKIPPED)


class CliTest(unittest.TestCase):
    def test_format(self) -> None:
        lines = cli.format_selftest(
            [
                {
                    "account": "acc",
                    "username": "user1",
                    "session": "ok",
                    "pairs": [
                        {"bot": A, "status": "reply_received", "closed": True, "seconds": 3.2},
                        {"bot": B, "status": "error", "detail": "ValueError", "closed": None},
                    ],
                }
            ]
        )
        self.assertEqual(len(lines), 3)
        self.assertIn("reply_received", lines[1])
        self.assertIn("(ValueError)", lines[2])

    def test_http_error_detail(self) -> None:
        import io
        import urllib.error

        err = urllib.error.HTTPError(
            "u",
            409,
            "Conflict",
            None,
            io.BytesIO(b'{"detail": "already running"}'),  # type: ignore[arg-type]
        )
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as ctx:
                cli._selftest(
                    SimpleNamespace(
                        account=None,
                        bot=None,
                        dry_run=True,
                        text="x",
                        search_timeout=1,
                        reply_timeout=1,
                        close_timeout=1,
                    )
                )
        self.assertEqual(str(ctx.exception), "Selftest failed: HTTP 409 already running")

    def test_argparse(self) -> None:
        argv = ["bot", "selftest", "--dry-run", "--account", "a", "--bot", "@b"]
        with (
            patch("sys.argv", argv),
            patch.object(cli, "_selftest") as run,
            patch.object(cli, "setup_logging"),
        ):
            cli.main()
        args = run.call_args.args[0]
        self.assertTrue(args.dry_run)
        self.assertEqual((args.account, args.bot, args.text), (["a"], ["@b"], "Привет"))


if __name__ == "__main__":
    unittest.main()
