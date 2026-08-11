from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import run_bots


class RunBotsSupervisorTests(unittest.TestCase):
    def test_launcher_exits_for_restart_when_all_bot_threads_die(self) -> None:
        config = SimpleNamespace(validate=lambda: (True, ""))
        manager = MagicMock()
        manager.start.return_value = (["Default"], [])
        manager.running_count.return_value = 0
        manager.stop_all.return_value = []
        stop_event = MagicMock()
        stop_event.wait.return_value = False

        with (
            patch.object(run_bots, "load_config", return_value=config),
            patch.object(run_bots, "BotRuntimeManager", return_value=manager),
            patch.object(run_bots.threading, "Event", return_value=stop_event),
            patch.object(run_bots.signal, "signal"),
            patch.object(run_bots, "log"),
        ):
            self.assertEqual(run_bots.main(), 1)

        manager.stop_all.assert_called_once_with()

    def test_launcher_stops_runtimes_when_shutdown_is_requested(self) -> None:
        config = SimpleNamespace(validate=lambda: (True, ""))
        manager = MagicMock()
        manager.start.return_value = (["Default"], [])
        manager.stop_all.return_value = []
        stop_event = MagicMock()
        stop_event.wait.return_value = True

        with (
            patch.object(run_bots, "load_config", return_value=config),
            patch.object(run_bots, "BotRuntimeManager", return_value=manager),
            patch.object(run_bots.threading, "Event", return_value=stop_event),
            patch.object(run_bots.signal, "signal"),
        ):
            self.assertEqual(run_bots.main(), 0)

        manager.stop_all.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
