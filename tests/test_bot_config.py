import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings

NEW_BOTS = ["@MessageAnonBot", "@nektome_mibot", "@secretme"]


def resolve(items: list[dict], **env) -> tuple[Settings, list]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.json"
        path.write_text(json.dumps(items), encoding="utf-8")
        settings = Settings(api_id=1, api_hash="h", accounts_file=path, _env_file=None, **env)
        return settings, settings.resolve_accounts()


def example_items() -> list[dict]:
    root = Path(__file__).resolve().parent.parent
    return json.loads((root / "accounts.example.json").read_text(encoding="utf-8"))


class BotListTest(unittest.TestCase):
    def test_default_list(self) -> None:
        _, (acc,) = resolve([{"id": "main"}])
        self.assertEqual([b.username for b in acc.bots], ["@AnonRuBot", *NEW_BOTS])

    def test_primary_first_and_dedup(self) -> None:
        _, (acc,) = resolve(
            [{"id": "x", "anon_bot_username": "@messageanonbot", "anon_bots": ["MessageAnonBot"]}]
        )
        self.assertEqual([b.username for b in acc.bots], ["@messageanonbot"])

    def test_empty_list_means_no_fallbacks(self) -> None:
        _, (acc,) = resolve([{"id": "x", "anon_bots": []}])
        self.assertEqual([b.username for b in acc.bots], ["@AnonRuBot"])

    def test_example_file_spec1(self) -> None:
        _, accounts = resolve(example_items())
        spec1 = next(a for a in accounts if a.id == "spec1")
        self.assertEqual([b.username for b in spec1.bots], ["@chatruIetkabot", *NEW_BOTS])
        first = spec1.bots[0]
        self.assertEqual(first.search_command, "/next")
        self.assertEqual(first.partner_found_pattern, "начинай общение")
        self.assertTrue(first.service_pattern)
        self.assertEqual(first.reply_timeout_seconds, 0)
        for acc in accounts:
            self.assertEqual(len(acc.bots), 4)

    def test_new_bots_get_default_profile(self) -> None:
        settings, (acc,) = resolve([{"id": "x"}])
        old, new = acc.bots[0], acc.bots[1]
        self.assertEqual(old.partner_found_pattern, settings.anon_partner_found_pattern)
        self.assertEqual(old.reply_timeout_seconds, 0)
        self.assertEqual(new.search_command, "/next")
        self.assertNotEqual(new.partner_found_pattern, settings.anon_partner_found_pattern)
        self.assertEqual(new.reply_timeout_seconds, settings.anon_bot_reply_timeout_seconds)

    def test_profile_layers(self) -> None:
        env_profiles = {"@secretme": {"username": "@secretme", "search_command": "/find"}}
        _, (acc,) = resolve(
            [
                {
                    "id": "x",
                    "anon_bot_profiles": {
                        "@nektome_mibot": {"username": "@nektome_mibot", "search_command": "/go"}
                    },
                }
            ],
            anon_bot_profiles=env_profiles,
        )
        by_name = {b.username: b for b in acc.bots}
        self.assertEqual(by_name["@nektome_mibot"].search_command, "/go")
        self.assertEqual(by_name["@secretme"].search_command, "/find")
        self.assertEqual(by_name["@MessageAnonBot"].search_command, "/next")

    def test_primary_account_overrides_beat_profiles(self) -> None:
        _, (acc,) = resolve(
            [
                {
                    "id": "x",
                    "anon_bot_username": "@secretme",
                    "anon_search_command": "/next",
                    "anon_bot_profiles": {
                        "secretme": {"username": "@secretme", "search_command": "/p"}
                    },
                }
            ]
        )
        self.assertEqual(acc.bots[0].search_command, "/next")

    def test_bad_profile_pattern(self) -> None:
        with self.assertRaisesRegex(ValueError, "bad"):
            resolve(
                [
                    {
                        "id": "x",
                        "anon_bot_profiles": {
                            "b": {"username": "@b", "limit_pattern": "(unclosed"}
                        },
                    }
                ]
            )

    def test_env_json(self) -> None:
        import os

        os.environ["ANON_FALLBACK_BOTS"] = '["@one"]'
        os.environ["ANON_BOT_PROFILES"] = '{"one": {"username": "@one", "search_command": "/o"}}'
        try:
            _, (acc,) = resolve([{"id": "x"}], **{})
        finally:
            del os.environ["ANON_FALLBACK_BOTS"]
            del os.environ["ANON_BOT_PROFILES"]
        self.assertEqual([b.username for b in acc.bots], ["@AnonRuBot", "@one"])
        self.assertEqual(acc.bots[1].search_command, "/o")
