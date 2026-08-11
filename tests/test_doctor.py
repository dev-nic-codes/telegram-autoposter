from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.doctor import check_media_tools, load_runtime_configs


class DoctorTests(unittest.TestCase):
    def test_valid_config_loads_as_one_runtime_profile(self) -> None:
        data = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
        data.update(
            {
                "bot_token": "example-token",
                "admin_chat_id": 123,
                "admin_user_ids": [123],
                "channels": [{"username": "@example", "description": "Example"}],
                "reddit_client_id": "client-id",
                "reddit_client_secret": "client-secret",
                "user_agent": "linux:telegram-autoposter:v2.1 (by /u/example)",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            profiles = load_runtime_configs(path)
        self.assertEqual(len(profiles), 1)

    def test_media_tool_check_reports_only_missing_commands(self) -> None:
        with patch("scripts.doctor.shutil.which", side_effect=lambda name: "/bin/ffmpeg" if name == "ffmpeg" else None):
            self.assertEqual(check_media_tools(), ["ffprobe"])


if __name__ == "__main__":
    unittest.main()
