from __future__ import annotations

import sys
import unittest
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from bot import RedditTelegramBot  # noqa: E402
from config import Config  # noqa: E402


class BotRuntimeTests(unittest.TestCase):
    def test_automatic_daily_digest_is_not_scheduled_or_toggleable(self) -> None:
        self.assertFalse(hasattr(RedditTelegramBot, "_daily_digest_due"))
        self.assertFalse(hasattr(RedditTelegramBot, "_send_daily_digest_if_due"))
        self.assertNotIn("daily_digest_enabled", RedditTelegramBot.ADMIN_MENU_TOGGLES)
        self.assertFalse(hasattr(Config(), "daily_digest_enabled"))


if __name__ == "__main__":
    unittest.main()
