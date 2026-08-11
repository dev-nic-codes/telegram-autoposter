from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))

from url_utils import path_for_domain, url_matches_domain


class URLUtilsTests(unittest.TestCase):
    def test_domain_and_subdomain_are_accepted(self) -> None:
        self.assertTrue(url_matches_domain("https://reddit.com/r/cats", "reddit.com"))
        self.assertTrue(url_matches_domain("https://www.reddit.com/r/cats", "reddit.com"))

    def test_lookalike_domains_are_rejected(self) -> None:
        self.assertFalse(url_matches_domain("https://evilreddit.com/r/cats", "reddit.com"))
        self.assertFalse(url_matches_domain("https://reddit.com.attacker.example/r/cats", "reddit.com"))

    def test_bare_domain_path_can_be_normalized(self) -> None:
        self.assertEqual(
            path_for_domain("reddit.com/r/cats/comments/abc", "reddit.com", allow_bare_host=True),
            "/r/cats/comments/abc",
        )

    def test_relative_path_is_not_treated_as_a_domain(self) -> None:
        self.assertIsNone(path_for_domain("/r/cats/comments/abc", "reddit.com", allow_bare_host=True))


if __name__ == "__main__":
    unittest.main()
