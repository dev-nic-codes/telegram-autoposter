from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from state_manager import StateManager  # noqa: E402
from traffic_service import TrafficService  # noqa: E402


@unittest.skipIf(os.name == "nt", "POSIX permission bits are not available on Windows")
class RuntimeStoragePermissionsTests(unittest.TestCase):
    def test_state_and_backup_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            manager = StateManager(str(state_path))

            self.assertTrue(manager.save())
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertTrue(manager.backup())

            backups = list(Path(directory).glob("state.json.backup_*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_traffic_database_and_directory_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = TrafficService(SimpleNamespace(profile_key="test", profile_name="Test"))
            service.db_path = Path(directory) / "states" / "traffic.sqlite"

            service.initialize()

            self.assertEqual(stat.S_IMODE(service.db_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(service.db_path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
