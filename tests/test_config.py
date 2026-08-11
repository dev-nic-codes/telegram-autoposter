from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import Config


class ConfigTests(unittest.TestCase):
    @staticmethod
    def configured() -> Config:
        config = Config()
        config.bot_token = "example-token"
        config.admin_chat_id = 123
        config.channels = [{"username": "@example", "description": "Example"}]
        config.subreddits = ["cats"]
        config.user_agent = "linux:telegram-autoposter:v2.1 (by /u/example)"
        return config

    def test_public_examples_are_valid_json_and_contain_placeholders(self) -> None:
        for filename in ("config.example.json", "config.multibot.example.json"):
            data = json.loads(Path(filename).read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)
            rendered = json.dumps(data)
            self.assertNotRegex(rendered, r"[0-9]{8,12}:[A-Za-z0-9_-]{30,50}")

    def test_single_bot_config_loads_without_touching_runtime_files(self) -> None:
        source = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            config = Config(str(path))
            self.assertTrue(config.load())
            self.assertFalse(config.is_multi_bot_config())

    def test_subreddit_names_are_normalized(self) -> None:
        config = Config()
        self.assertEqual(config.normalize_subreddit_name(" /r/Cats "), "cats")
        self.assertEqual(config.normalize_subreddit_name("r/Dogs"), "dogs")

    def test_admin_ids_are_positive_and_deduplicated(self) -> None:
        config = Config()
        config.admin_chat_id = 123
        config.admin_user_ids = [123, 456, 0, -1]
        self.assertEqual(config.get_admin_user_ids(), [123, 456])

    def test_valid_single_profile_passes_validation(self) -> None:
        self.assertEqual(self.configured().validate(), (True, "Configuration is valid"))

    def test_partial_reddit_credentials_are_rejected(self) -> None:
        config = self.configured()
        config.reddit_client_id = "client-id"
        self.assertEqual(
            config.validate(),
            (False, "Reddit OAuth requires both reddit_client_id and reddit_client_secret"),
        )

    def test_download_limit_above_telegram_limit_is_rejected(self) -> None:
        config = self.configured()
        config.max_download_mb = 51
        self.assertEqual(
            config.validate(),
            (False, "Max download size cannot exceed 50 MB (Telegram limit)"),
        )

    def test_default_channel_is_normalized(self) -> None:
        config = Config()
        config.set_default_channel("example")
        self.assertEqual(config.get_default_channel(), "@example")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not available on Windows")
    def test_save_is_atomic_and_restricts_config_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"sentinel": true}', encoding="utf-8")
            config = Config(str(path))

            self.assertTrue(config.save())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_failed_atomic_replace_preserves_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = '{"sentinel": true}\n'
            path.write_text(original, encoding="utf-8")
            config = Config(str(path))

            with patch("src.config.os.replace", side_effect=OSError("simulated failure")):
                self.assertFalse(config.save())

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(directory).glob(".config.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
