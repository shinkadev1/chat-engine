import asyncio
import logging
import unittest
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr
from test_selftest import LIMIT_TEXT, A, B, FakeClient

from app import healthcheck, notify, selftest
from app.config import Settings
from app.services.anon_chat import AnonChatService

WHEN = datetime(2026, 11, 17, 11, 0, tzinfo=UTC)  # 14:00 МСК


def build(script=None, *, acc_id="acc", bots=(A, B), reply_timeout=0):
    ref: list = []
    cfgs = [
        SimpleNamespace(
            username=name,
            search_command="/search",
            stop_command="/stop",
            partner_found_pattern="chat started",
            partner_left_pattern="partner left",
            limit_pattern="исчерпали дневной лимит",
            service_pattern="searching",
            stop_ack_pattern="you stopped",
            reply_timeout_seconds=reply_timeout,
            search_prompt_pattern="which chat",
            search_prompt_button="yes",
        )
        for name in bots
    ]
    config = SimpleNamespace(id=acc_id, bots=cfgs)
    client = FakeClient(ref, script)
    service = AnonChatService(client, Settings(_env_file=None), bots[0], account=config)  # type: ignore[arg-type]
    ref.append(service)
    account = SimpleNamespace(id=acc_id, client=client, service=service, config=config)
    return account, service, client


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


class HealthcheckTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_sends_only_stop(self) -> None:
        account, service, client = build({(A, "/stop"): ["Сейчас нет разговора"]})
        report = await healthcheck.run_healthcheck([account], probe_wait=0.2, now=WHEN)
        self.assertEqual(client.sent, [(A, "/stop"), (B, "/stop")])
        self.assertEqual([b.mark for b in report.accounts[0].bots], ["+", "-"])
        self.assertFalse(service.probing)

    async def test_mixed_format(self) -> None:
        main, main_service, _ = build(
            {(A, "/stop"): [LIMIT_TEXT], (B, "/stop"): ["нет разговора"]}, acc_id="main"
        )
        second, _, _ = build(acc_id="second")
        second.client.authorized = False
        report = await healthcheck.run_healthcheck([main, second], probe_wait=0.2, now=WHEN)
        self.assertEqual(
            report.text,
            "Проверка chat-engine 17.11 14:00 (МСК)\n"
            "Аккаунтов активно: 1/2, чатов работает: 1/4\n"
            "main -> bot_a(лимит), bot_b(+)\n"
            "second -> не активен (не авторизован)",
        )
        self.assertEqual((report.working, report.total), (1, 4))

    async def test_limit_blocked_no_probe(self) -> None:
        account, service, client = build({(B, "/stop"): ["ok"]})
        await service.handle_bot_message(LIMIT_TEXT, B)
        report = await healthcheck.run_healthcheck([account], probe_wait=0.2, now=WHEN)
        self.assertEqual(report.accounts[0].bots[1].mark, "лимит")
        self.assertEqual(client.sent, [(A, "/stop")])

    async def test_busy_uses_recent_activity(self) -> None:
        account, service, client = build()
        await service.start_chat()
        client.sent.clear()
        report = await healthcheck.run_healthcheck([account], probe_wait=0.2, now=WHEN)
        self.assertEqual(client.sent, [])
        item = report.accounts[0]
        self.assertTrue(item.busy)
        self.assertEqual([b.mark for b in item.bots], ["+", "?"])
        self.assertTrue(report.text.endswith("(в диалоге)"))
        await service.shutdown()

    async def test_recent_success_skips_probe(self) -> None:
        account, service, client = build()
        await service.start_chat()
        await service.handle_bot_message("chat started", A)
        self.assertIsNotNone(service.last_partner_found_at(A))
        await service.stop_chat()
        client.sent.clear()
        report = await healthcheck.run_healthcheck([account], probe_wait=0.2, now=WHEN)
        self.assertEqual(client.sent, [(B, "/stop")])
        self.assertEqual(report.accounts[0].bots[0].mark, "+")
        await service.shutdown()

    async def test_found_not_set_by_probe(self) -> None:
        account, service, _ = build()
        await service.begin_probe()
        await service.handle_bot_message("chat started", A)
        service.end_probe()
        self.assertIsNone(service.last_partner_found_at(A))

    async def test_skipped_when_lock_held(self) -> None:
        account, _, client = build()
        async with selftest._RUN_LOCK:
            with self.assertRaises(selftest.SelftestBusyError):
                await healthcheck.run_healthcheck([account])
        self.assertEqual(client.sent, [])

    async def test_scheduler_disabled(self) -> None:
        token = SecretStr("123:abc")
        for conf in (
            settings(selftest_notify_bot_token=token, selftest_notify_chat_id="-1"),
            settings(selftest_interval_hours=4, selftest_notify_chat_id="-1"),
            settings(selftest_interval_hours=4, selftest_notify_bot_token=token),
        ):
            self.assertIsNone(healthcheck.start_scheduler(SimpleNamespace(), conf))

    async def test_scheduler_runs_and_cancels(self) -> None:
        conf = settings(
            selftest_interval_hours=4,
            selftest_notify_bot_token=SecretStr("123:abc"),
            selftest_notify_chat_id="-1",
        )
        account, _, _ = build()
        manager = SimpleNamespace(list=lambda: [account])
        sent: list[str] = []

        async def fake_send(token, chat, text, **kw):
            sent.append(text)
            return True

        with (
            patch.object(healthcheck, "send_telegram", fake_send),
            patch.object(healthcheck, "PROBE_WAIT", 0.1),
        ):
            task = healthcheck.start_scheduler(manager, conf, first_delay=0)
            assert task is not None
            for _ in range(50):
                await asyncio.sleep(0.05)
                if sent:
                    break
            await healthcheck.stop_scheduler(task)
        self.assertEqual(len(sent), 1)
        self.assertTrue(task.cancelled() or task.done())


class ProbeGuardsTest(unittest.IsolatedAsyncioTestCase):
    async def test_light_probe_does_not_press_prompt(self) -> None:
        _, service, _ = build()
        clicks: list = []

        async def click(pattern):
            clicks.append(pattern)
            return True

        await service.begin_probe(press_prompt=False)
        await service.handle_bot_message("which chat?", A, click=click)
        self.assertEqual(clicks, [])
        service.end_probe()
        await service.begin_probe()
        await service.handle_bot_message("which chat?", A, click=click)
        self.assertEqual(len(clicks), 1)
        service.end_probe()

    async def test_late_stop_ack_keeps_reply_timer(self) -> None:
        _, service, _ = build(reply_timeout=30)
        await service.start_chat()
        task = service._reply_task
        self.assertIsNotNone(task)
        await service.handle_bot_message("you stopped", A)
        await service.handle_bot_message("searching", A)
        self.assertIs(service._reply_task, task)
        self.assertFalse(task.cancelled())
        await service.handle_bot_message("hello?", A)
        self.assertIsNone(service._reply_task)
        await service.shutdown()


class NotifyTest(unittest.TestCase):
    def test_request_and_no_token_in_logs(self) -> None:
        captured = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout):
            captured.append((request, timeout))
            return Resp()

        with (
            patch("app.notify.urllib.request.urlopen", fake_urlopen),
            self.assertLogs("app.notify", level=logging.INFO) as logs,
        ):
            self.assertTrue(notify.send_sync("123:SECRET", "-100", "привет"))
        request, timeout = captured[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot123:SECRET/sendMessage")
        self.assertEqual(timeout, 15)
        self.assertIn(b"-100", request.data)
        self.assertNotIn("SECRET", "\n".join(logs.output))
        self.assertNotIn("api.telegram.org", "\n".join(logs.output))

    def test_retries_and_failure_logged_without_token(self) -> None:
        calls = []

        def boom(request, timeout):
            calls.append(1)
            raise urllib.error.URLError("http://api.telegram.org/botSECRET")

        with (
            patch("app.notify.urllib.request.urlopen", boom),
            self.assertLogs("app.notify", level=logging.WARNING) as logs,
        ):
            self.assertFalse(notify.send_sync("SECRET", "1", "x", delay=0))
        self.assertEqual(len(calls), 3)
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_split(self) -> None:
        text = "\n".join(["x" * 100] * 100)
        parts = notify.split_text(text)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual("\n".join(parts), text)
        self.assertEqual(notify.split_text("abc"), ["abc"])
