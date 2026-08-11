from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from telegram_handler import TelegramAPIError, TelegramHandler  # noqa: E402


class TelegramHandlerReliabilityTests(unittest.TestCase):
    def test_request_errors_do_not_expose_bot_token(self) -> None:
        token = "123456:super-secret-token"
        handler = TelegramHandler(token)
        error = requests.ConnectionError(f"connection failed for https://api.telegram.org/bot{token}/getMe")

        with patch("telegram_handler.requests.get", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                handler.get_me()

        message = str(raised.exception)
        self.assertNotIn(token, message)
        self.assertIn("<redacted>", message)

    def test_get_updates_uses_bounded_exponential_backoff_and_resets(self) -> None:
        handler = TelegramHandler("test-token")

        with (
            patch.object(
                handler,
                "_get",
                side_effect=[RuntimeError("temporary failure"), RuntimeError("temporary failure"), {"result": []}],
            ),
            patch("telegram_handler.log"),
        ):
            self.assertEqual(handler.get_updates(), [])
            self.assertEqual(handler.get_updates_backoff_seconds, 1.0)
            self.assertEqual(handler.get_updates(), [])
            self.assertEqual(handler.get_updates_backoff_seconds, 2.0)
            self.assertEqual(handler.get_updates(), [])

        self.assertEqual(handler.get_updates_backoff_seconds, 0.0)

    def test_get_updates_honors_telegram_retry_after(self) -> None:
        handler = TelegramHandler("test-token")
        error = TelegramAPIError(
            "getUpdates",
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )

        with patch.object(handler, "_get", side_effect=error), patch("telegram_handler.log"):
            self.assertEqual(handler.get_updates(), [])

        self.assertEqual(handler.get_updates_backoff_seconds, 17.0)


if __name__ == "__main__":
    unittest.main()
