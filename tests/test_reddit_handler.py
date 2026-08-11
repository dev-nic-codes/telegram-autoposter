from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from reddit_handler import RedditHandler  # noqa: E402


class RedditRssSecurityTests(unittest.TestCase):
    def test_rss_rejects_external_entity_documents(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response._content = b"""<?xml version='1.0'?>
<!DOCTYPE feed [<!ENTITY secret SYSTEM 'file:///etc/passwd'>]>
<feed xmlns='http://www.w3.org/2005/Atom'><title>&secret;</title></feed>
"""
        handler = RedditHandler("telegram-autoposter-tests/1.0")

        with patch.object(handler.session, "get", return_value=response):
            with self.assertRaises(requests.HTTPError) as raised:
                handler._fetch_subreddit_rss("cats", limit=10, timeout=1)

        self.assertIn("RSS parse error", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
