"""
Non-interactive runtime launcher for configured AutoPoster bots.

This avoids the interactive menu in src/main.py and is safer for keeping
the live bot processes running in the background.
"""

import os
import signal
import sys
import threading


APP_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(APP_BASE_DIR, "src")

if APP_BASE_DIR not in sys.path:
    sys.path.insert(0, APP_BASE_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.main import BotRuntimeManager, load_config  # noqa: E402
from src.utils import log  # noqa: E402


def main() -> int:
    cfg = load_config()
    valid, error = cfg.validate()
    if not valid:
        print(f"Configuration error: {error}")
        return 1

    runtime_manager = BotRuntimeManager()
    started, already_running = runtime_manager.start(cfg)

    started_text = ", ".join(started) if started else "none"
    running_text = ", ".join(already_running) if already_running else "none"
    print(f"Started bots: {started_text}")
    print(f"Already running: {running_text}")

    stop_event = threading.Event()

    def request_stop(signum, _frame) -> None:
        log(f"Received signal {signum}; stopping AutoPoster runtimes...", "INFO")
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_stop)

    exit_code = 0
    try:
        while not stop_event.wait(2):
            if runtime_manager.running_count() == 0:
                log("No AutoPoster runtimes are running; exiting for supervisor restart", "ERROR")
                exit_code = 1
                break
    except KeyboardInterrupt:
        log("Stopping AutoPoster runtimes...", "INFO")
    finally:
        still_running = runtime_manager.stop_all()
        if still_running:
            log(f"Runtimes still stopping: {', '.join(still_running)}", "WARN")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
