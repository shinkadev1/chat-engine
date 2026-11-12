import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from app.api.routers.accounts import list_accounts
from app.config import AccountConfig, Settings


def make_settings(tmp_path_file, items) -> Settings:
    tmp_path_file.write_text(json.dumps(items), encoding="utf-8")
    return Settings(api_id=1, api_hash="h", accounts_file=tmp_path_file, _env_file=None)


class AccountModeTests(unittest.TestCase):
    def test_default_mode_is_normal(self) -> None:
        self.assertEqual(AccountConfig(id="main").mode, "normal")

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            AccountConfig.model_validate({"id": "x", "mode": "weird"})

    def test_resolve_passes_mode(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(
                Path(tmp) / "a.json",
                [
                    {"id": "main"},
                    {"id": "spec1", "anon_bot_username": "@chatruIetkabot", "mode": "adult"},
                ],
            )
            resolved = {a.id: a for a in settings.resolve_accounts()}
        self.assertEqual(resolved["main"].mode, "normal")
        self.assertEqual(resolved["spec1"].mode, "adult")
        self.assertEqual(resolved["spec1"].anon_bot_username, "@chatruIetkabot")

    def test_invalid_mode_in_file_fails(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp) / "a.json", [{"id": "x", "mode": "bad"}])
            with self.assertRaises(ValidationError):
                settings.resolve_accounts()


class AccountsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_includes_mode(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(
                Path(tmp) / "a.json", [{"id": "spec1", "mode": "adult"}, {"id": "main"}]
            )
            configs = settings.resolve_accounts()
        accounts = [
            SimpleNamespace(id=c.id, authorized=False, username=None, config=c, service=None)
            for c in configs
        ]
        manager = SimpleNamespace(list=lambda: accounts)
        out = await list_accounts(manager)
        self.assertEqual({a.id: a.mode for a in out}, {"spec1": "adult", "main": "normal"})


if __name__ == "__main__":
    unittest.main()
