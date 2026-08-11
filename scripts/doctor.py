#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for import_path in (PROJECT_ROOT, SRC_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))


def load_runtime_configs(config_path: Path):
    from config import Config

    config = Config(str(config_path))
    if not config.load():
        raise RuntimeError(f"could not load {config_path}")
    valid, error = config.validate()
    if not valid:
        raise RuntimeError(error)
    return config.build_runtime_configs()


def check_media_tools() -> list[str]:
    return [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]


def run_online_checks(runtime_configs) -> list[str]:
    from telegram_handler import TelegramHandler

    errors: list[str] = []
    for config in runtime_configs:
        label = config.profile_name or config.profile_key or "Default"
        telegram = TelegramHandler(config.bot_token)
        try:
            me = telegram.get_me().get("result", {})
            bot_id = int(me.get("id") or 0)
            print(f"OK Telegram bot [{label}]: @{me.get('username') or 'unnamed_bot'}")
        except Exception as exc:
            errors.append(f"Telegram bot [{label}]: {telegram._safe_exception_text(exc)}")
            continue

        for channel in config.channels:
            username = str(channel.get("username") or "").strip()
            if not username:
                errors.append(f"Telegram channel [{label}]: empty username")
                continue
            valid, detail = telegram.test_is_channel_admin(username, bot_id)
            if valid:
                print(f"OK Telegram destination [{label}]: {username}")
            else:
                errors.append(f"Telegram destination [{label}] {username}: {detail}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an AutoPoster installation.")
    parser.add_argument("--config", default="config.json", help="configuration file to validate")
    parser.add_argument("--online", action="store_true", help="also verify Telegram access")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    errors: list[str] = []
    try:
        runtime_configs = load_runtime_configs(config_path)
        print(f"OK configuration: {len(runtime_configs)} bot profile(s)")
    except Exception as exc:
        print(f"FAIL configuration: {exc}")
        return 1

    missing_tools = check_media_tools()
    if missing_tools:
        errors.append(f"missing media tools: {', '.join(missing_tools)}")
    else:
        print("OK media tools: ffmpeg and ffprobe")

    for config in runtime_configs:
        if not config.has_oauth_credentials():
            errors.append(f"Reddit OAuth is not configured for {config.profile_name or 'Default'}")

    if args.online:
        errors.extend(run_online_checks(runtime_configs))

    if errors:
        print("\nChecks failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("\nAll installation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
