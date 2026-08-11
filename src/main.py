"""
Entry point for Telegram Autoposter.
Adds an interactive main menu before starting automation.
"""

import ctypes
import copy
import json
import os
import sys
import threading
import traceback
import webbrowser
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bot import RedditTelegramBot
from commands import CommandHandler
from config import Config
from dashboard_server import LocalDashboardServer
from scheduler import Scheduler
from state_manager import StateManager
from telegram_handler import TelegramHandler
from utils import get_log_history, log

HEADER = "TELEGRAM AUTOPOSTER"

TAGLINE = (
    "Telegram Autoposter - automated Reddit-to-Telegram posting engine.\n"
    "Built for reliability, control, and long-term uptime.\n"
    "Created by Nic."
)

# ANSI color helpers
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
FG_CYAN = "\x1b[36m"
FG_GREEN = "\x1b[32m"
FG_YELLOW = "\x1b[33m"
FG_RED = "\x1b[31m"
FG_MAGENTA = "\x1b[35m"
FG_WHITE = "\x1b[37m"


def colorize(text: str, *styles: str) -> str:
    """Wrap text in ANSI styles."""
    if not styles:
        return text
    return f"{''.join(styles)}{text}{RESET}"


def enable_utf8_console() -> None:
    """Best-effort UTF-8 console configuration to avoid banner crashes on Windows."""
    try:
        if os.name == "nt":
            try:
                kernel32 = ctypes.windll.kernel32
                # 65001 = UTF-8 code page
                kernel32.SetConsoleOutputCP(65001)
                kernel32.SetConsoleCP(65001)
            except Exception:
                pass
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # If this fails, we still continue; the user can use chcp 65001.
        pass


def enable_ansi_colors() -> None:
    """Enable ANSI colors on Windows terminals (best effort)."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        # If this fails, output will remain uncolored.
        pass


def _is_frozen() -> bool:
    """Return True when running from a PyInstaller-style EXE."""
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> str:
    """Directory containing the executable."""
    return os.path.dirname(os.path.abspath(sys.executable))


def _project_root_dir() -> str:
    """
    Best-effort project root / data directory.
    - In source runs: parent of this src/ folder.
    - In EXE runs: keep data next to the EXE, except when running from release/dist.
    """
    if _is_frozen():
        exe_dir = _exe_dir()
        parent = os.path.abspath(os.path.join(exe_dir, ".."))
        exe_dir_name = os.path.basename(exe_dir).lower()
        if exe_dir_name in {"release", "dist"} and os.path.isdir(os.path.join(parent, "src")):
            return parent
        return exe_dir
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _candidate_dirs() -> list[str]:
    """Directories to search for existing config/state files."""
    dirs: list[str] = [os.getcwd(), _project_root_dir()]
    if _is_frozen():
        exe_dir = _exe_dir()
        parent = os.path.abspath(os.path.join(exe_dir, ".."))
        dirs.extend([exe_dir, parent])

    seen: set[str] = set()
    ordered: list[str] = []
    for path in dirs:
        if not path or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def _resolve_base_dir() -> str:
    """Pick the directory that should hold config/state files."""
    for path in _candidate_dirs():
        if os.path.exists(os.path.join(path, "config.json")):
            return path
    return _project_root_dir()


APP_BASE_DIR = _resolve_base_dir()
CONFIG_PATH = os.path.join(APP_BASE_DIR, "config.json")
STATE_PATH = os.path.join(APP_BASE_DIR, "state.json")
MULTI_STATE_DIR = os.path.join(APP_BASE_DIR, "states")
CONFIG_BACKUP_DIR = os.path.join(APP_BASE_DIR, "backups")


def check_setup() -> bool:
    """Check if config exists."""
    return os.path.exists(CONFIG_PATH)


def run_setup_wizard() -> bool:
    """Run the interactive setup wizard inline (best effort)."""
    try:
        from setup_wizard import SetupWizard
    except Exception as exc:
        print(f"Failed to load setup wizard: {exc}")
        log(f"Failed to load setup wizard: {exc}")
        return False

    try:
        wizard = SetupWizard(CONFIG_PATH)
        return bool(wizard.run())
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        return False
    except Exception as exc:
        print(f"\nSetup failed: {exc}")
        log(f"Setup failed: {exc}")
        return False


def load_config() -> Config:
    """Load configuration from disk."""
    cfg = Config(CONFIG_PATH)
    cfg.load()
    return cfg


def load_state(state_path: str = STATE_PATH) -> StateManager:
    """Load state from disk."""
    st = StateManager(state_path)
    st.load(quiet=True)
    return st


def _resolve_runtime_state_path(bot_cfg: Config, *, multi_bot: bool, index: int) -> str:
    """Resolve the state file path for a specific bot profile."""
    if bot_cfg.state_file_override:
        if os.path.isabs(bot_cfg.state_file_override):
            return os.path.abspath(bot_cfg.state_file_override)
        return os.path.abspath(os.path.join(APP_BASE_DIR, bot_cfg.state_file_override))

    if not multi_bot:
        return STATE_PATH

    default_path = os.path.join(MULTI_STATE_DIR, f"{bot_cfg.profile_key}.json")

    # Preserve the current single-bot state for the first profile when migrating.
    if index == 1 and os.path.exists(STATE_PATH) and not os.path.exists(default_path):
        return STATE_PATH

    return default_path


def build_runtime_entries(cfg: Config) -> List[Dict[str, Any]]:
    """Build runtime metadata for each configured bot profile."""
    runtime_configs = cfg.build_runtime_configs()
    multi_bot = cfg.is_multi_bot_config()
    entries: List[Dict[str, Any]] = []

    for index, bot_cfg in enumerate(runtime_configs, 1):
        state_path = _resolve_runtime_state_path(bot_cfg, multi_bot=multi_bot, index=index)
        entries.append(
            {
                "key": bot_cfg.profile_key,
                "config": bot_cfg,
                "state_path": os.path.abspath(state_path),
            }
        )

    return entries


def load_runtime_entries(cfg: Config) -> List[Dict[str, Any]]:
    """Load state for each configured runtime entry."""
    entries = build_runtime_entries(cfg)
    for entry in entries:
        entry["state"] = load_state(entry["state_path"])
    return entries


def set_runtime_paused(cfg: Config, paused: bool) -> None:
    """Persist paused state across all configured bot profiles."""
    for entry in build_runtime_entries(cfg):
        st = load_state(entry["state_path"])
        st.set_paused(paused)
        st.save()


def _collect_known_state_paths(cfg: Config) -> List[str]:
    """Collect the known state file paths for cleanup and statistics."""
    paths = {STATE_PATH}
    for entry in build_runtime_entries(cfg):
        paths.add(entry["state_path"])

    if os.path.isdir(MULTI_STATE_DIR):
        for name in os.listdir(MULTI_STATE_DIR):
            if name.lower().endswith(".json"):
                paths.add(os.path.join(MULTI_STATE_DIR, name))

    return sorted(os.path.abspath(path) for path in paths)


def _timestamp_slug() -> str:
    """Return a filesystem-safe UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _safe_filename_part(value: str, fallback: str = "backup") -> str:
    """Return a conservative filename segment."""
    cleaned = []
    for ch in str(value or "").strip().lower():
        if ch.isalnum() or ch in {"-", "_"}:
            cleaned.append(ch)
        elif ch in {" ", ".", ":"}:
            cleaned.append("-")
    result = "".join(cleaned).strip("-_")
    while "--" in result:
        result = result.replace("--", "-")
    return result or fallback


def _is_within_dir(path: str, root: str) -> bool:
    """Return True when path resolves inside root."""
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def _normalize_archive_name(name: str) -> str:
    """Normalize a ZIP member name for backup/restore checks."""
    normalized = str(name or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _is_allowed_backup_member(name: str) -> bool:
    """Return True for files this app is willing to restore."""
    normalized = _normalize_archive_name(name)
    if not normalized:
        return False
    if normalized in {"config.json", "state.json"}:
        return True
    return normalized.startswith("states/") and normalized.endswith(".json")


def _archive_name_for_path(path: str, base_dir: Optional[str] = None) -> str:
    """Return the backup archive name for a path inside the app data directory."""
    base_dir = base_dir or APP_BASE_DIR
    abs_path = os.path.abspath(path)
    if not _is_within_dir(abs_path, base_dir):
        return ""
    rel = os.path.relpath(abs_path, base_dir).replace("\\", "/")
    return rel if _is_allowed_backup_member(rel) else ""


def _next_available_path(path: str) -> str:
    """Avoid overwriting an existing generated file."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for index in range(2, 1000):
        candidate = f"{root}-{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{root}-{_timestamp_slug()}{ext}"


def _create_config_backup_archive(
    cfg: Config,
    *,
    label: str = "manual",
    include_state: bool = True,
    backup_dir: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Create a restorable ZIP backup of config and known state files."""
    backup_dir = backup_dir or CONFIG_BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)
    safe_label = _safe_filename_part(label)
    archive_path = _next_available_path(os.path.join(backup_dir, f"{_timestamp_slug()}-{safe_label}.zip"))

    files: List[Tuple[str, str]] = []
    if os.path.exists(CONFIG_PATH):
        arcname = _archive_name_for_path(CONFIG_PATH)
        if arcname:
            files.append((os.path.abspath(CONFIG_PATH), arcname))

    if include_state:
        seen_arcnames = {arcname for _, arcname in files}
        for state_path in _collect_known_state_paths(cfg):
            if not os.path.exists(state_path):
                continue
            arcname = _archive_name_for_path(state_path)
            if not arcname or arcname in seen_arcnames:
                continue
            files.append((os.path.abspath(state_path), arcname))
            seen_arcnames.add(arcname)

    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "include_state": include_state,
        "profile_count": len(build_runtime_entries(cfg)),
        "files": [arcname for _, arcname in files],
    }

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_path, arcname in files:
            archive.write(source_path, arcname)
        archive.writestr("metadata.json", json.dumps(metadata, indent=2))

    return archive_path, metadata["files"]


def _list_config_backups(backup_dir: Optional[str] = None) -> List[str]:
    """Return known backup ZIP files newest first."""
    backup_dir = backup_dir or CONFIG_BACKUP_DIR
    if not os.path.isdir(backup_dir):
        return []
    paths = [os.path.join(backup_dir, name) for name in os.listdir(backup_dir) if name.lower().endswith(".zip")]
    return sorted(paths, key=lambda path: os.path.getmtime(path), reverse=True)


def _describe_config_backup(path: str) -> str:
    """Return a compact, non-secret backup summary."""
    name = os.path.basename(path)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            metadata = {}
            if "metadata.json" in archive.namelist():
                metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
            files = metadata.get("files") or [item for item in archive.namelist() if _is_allowed_backup_member(item)]
            created = str(metadata.get("created_at_utc") or "").replace("T", " ")[:19]
            label = str(metadata.get("label") or "").strip()
            label_part = f" | {label}" if label else ""
            return f"{name}{label_part} | {created or 'unknown time'} | {len(files)} file(s)"
    except Exception as exc:
        return f"{name} | unreadable: {exc}"


def _restore_config_backup_archive(path: str) -> List[str]:
    """Restore allowed files from a backup ZIP into the app data directory."""
    if not zipfile.is_zipfile(path):
        raise ValueError("Selected file is not a valid backup ZIP.")

    restored: List[str] = []
    with zipfile.ZipFile(path, "r") as archive:
        member_map = {
            _normalize_archive_name(name): name for name in archive.namelist() if _normalize_archive_name(name)
        }
        allowed_names = sorted({name for name in member_map if _is_allowed_backup_member(name)})
        if "config.json" not in allowed_names:
            raise ValueError("Backup does not contain config.json.")

        config_data = json.loads(archive.read(member_map["config.json"]).decode("utf-8"))
        if not isinstance(config_data, dict):
            raise ValueError("Backup config.json is not a JSON object.")

        for name in allowed_names:
            dest = os.path.abspath(os.path.join(APP_BASE_DIR, *name.split("/")))
            if not _is_within_dir(dest, APP_BASE_DIR):
                raise ValueError(f"Unsafe backup path: {name}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with archive.open(member_map[name], "r") as source, open(dest, "wb") as target:
                target.write(source.read())
            restored.append(name)

    return restored


def _redact_config_value(key: str, value: Any) -> Any:
    """Redact sensitive config values for export."""
    key_lower = str(key or "").lower()
    if key_lower in {"bot_token", "admin_chat_id", "reddit_client_secret"}:
        return "REDACTED" if value not in ("", 0, None) else value
    if isinstance(value, dict):
        return {child_key: _redact_config_value(child_key, child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_config_value(key, item) for item in value]
    return value


def _export_redacted_config(backup_dir: Optional[str] = None) -> str:
    """Export config.json with bot tokens and admin IDs redacted."""
    backup_dir = backup_dir or CONFIG_BACKUP_DIR
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError("config.json not found.")
    os.makedirs(backup_dir, exist_ok=True)
    with open(CONFIG_PATH, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("config.json is not a JSON object.")

    redacted = {key: _redact_config_value(key, value) for key, value in data.items()}
    export_path = _next_available_path(os.path.join(backup_dir, f"{_timestamp_slug()}-config-export-redacted.json"))
    with open(export_path, "w", encoding="utf-8") as target:
        json.dump(redacted, target, indent=2, ensure_ascii=False)
    return export_path


def _duplicate_profile_in_config(
    cfg: Config,
    source_index: int,
    new_name: str,
) -> Tuple[bool, str]:
    """Duplicate one bot profile and persist the config."""
    if not cfg.is_multi_bot_config():
        cfg.bots = [_primary_bot_entry_from_single_config(cfg)]

    if source_index < 0 or source_index >= len(cfg.bots):
        return False, "Unknown source profile."

    profile_name = str(new_name or "").strip()
    if not profile_name:
        return False, "Profile name is required."

    profile_key = cfg._sanitize_profile_key(profile_name, "bot-copy")
    existing_keys = {entry["key"] for entry in build_runtime_entries(cfg)}
    if profile_key in existing_keys:
        return False, "That profile name conflicts with an existing bot."

    new_bot = copy.deepcopy(cfg.bots[source_index])
    new_bot["name"] = profile_name
    new_bot["id"] = profile_key
    new_bot["state_file"] = f"states/{profile_key}.json"
    cfg.bots.append(new_bot)

    if not cfg.save():
        return False, "Could not save duplicated profile."
    return True, profile_name


def _print_multi_bot_edit_hint() -> None:
    """Explain how per-bot settings should be edited."""
    print("This configuration uses multiple bot profiles.")
    print(f"Edit {CONFIG_PATH} to change per-bot tokens, channels, subreddits, and schedules.")


def _default_admin_chat_id(cfg: Config) -> int:
    """Find the best default admin chat id from the current config."""
    if cfg.admin_chat_id:
        return cfg.admin_chat_id

    for entry in build_runtime_entries(cfg):
        bot_cfg = entry["config"]
        if bot_cfg.admin_chat_id:
            return bot_cfg.admin_chat_id

    return 0


def _suggest_profile_name(channel: str, fallback: str = "Bot") -> str:
    """Suggest a profile name from the channel username."""
    value = (channel or "").strip().lstrip("@").strip()
    if not value:
        return fallback

    parts = [part for part in value.replace("-", " ").replace("_", " ").split() if part]
    if not parts:
        return fallback
    return " ".join(part.capitalize() for part in parts)


def _prompt_bot_token() -> Optional[str]:
    """Prompt for and verify a Telegram bot token."""
    print("Bot token setup:")
    print("1. Create the bot with @BotFather if needed.")
    print("2. Paste the token below.")
    print()

    while True:
        token = input("Enter bot token (blank to cancel): ").strip()
        if not token:
            return None

        print("Testing bot token...")
        telegram = TelegramHandler(token)
        valid, result = telegram.test_bot_token()
        if valid:
            print(f"Bot token valid. Username: @{result}")
            return token

        print(f"Invalid bot token: {result}")


def _prompt_admin_chat_id(token: str, default_admin_id: int = 0) -> Optional[int]:
    """Prompt for and verify the admin chat id for a bot."""
    print()
    print("Admin chat setup:")
    print("Start a chat with the new bot and send /start before testing.")

    default_label = str(default_admin_id) if default_admin_id else "none"
    while True:
        raw = input(f"Enter admin Telegram ID (blank uses {default_label}, 0 cancels): ").strip()
        if not raw:
            if default_admin_id:
                admin_id = default_admin_id
            else:
                print("Admin chat ID is required.")
                continue
        else:
            try:
                admin_id = int(raw)
            except ValueError:
                print("Please enter a valid numeric ID.")
                continue

            if admin_id == 0:
                return None

        print("Testing admin messaging...")
        telegram = TelegramHandler(token)
        valid, error = telegram.test_can_message_admin(admin_id)
        if valid:
            print("Admin messaging test successful.")
            return admin_id

        print(f"Cannot message admin: {error}")
        print("Make sure the admin has started a chat with this bot.")


def _prompt_channel_username() -> Optional[str]:
    """Prompt for a Telegram channel username."""
    while True:
        raw = input("Enter channel username (e.g. @mychannel, blank to cancel): ").strip()
        if not raw:
            return None

        if not raw.startswith("@"):
            raw = "@" + raw

        if len(raw) < 2:
            print("Please enter a valid channel username.")
            continue

        return raw


def _prompt_subreddits() -> Optional[List[str]]:
    """Prompt for one or more subreddit names."""
    print()
    print("Enter subreddit names without r/.")
    print("Press Enter on an empty line when done.")

    subreddits: List[str] = []
    while True:
        raw = input("Subreddit: ").strip()
        if not raw:
            break

        value = normalize_subreddit(raw)
        if not value:
            continue
        if value in subreddits:
            print(f"r/{value} is already listed.")
            continue
        subreddits.append(value)

    if not subreddits:
        print("At least one subreddit is required.")
        return None

    return subreddits


def _primary_bot_entry_from_single_config(cfg: Config) -> Dict[str, Any]:
    """Create the first bot profile when converting from single-bot mode."""
    channel = cfg.get_default_channel() or ""
    return {
        "name": _suggest_profile_name(channel, "Primary Bot"),
        "state_file": "state.json",
        "bot_token": cfg.bot_token,
        "admin_chat_id": cfg.admin_chat_id,
        "channels": [dict(item) for item in cfg.channels if isinstance(item, dict)],
        "subreddits": list(cfg.subreddits),
        "spoiler_posts_enabled": bool(getattr(cfg, "spoiler_posts_enabled", False)),
        "emergency_pause_enabled": bool(getattr(cfg, "emergency_pause_enabled", True)),
        "emergency_pause_window_minutes": int(getattr(cfg, "emergency_pause_window_minutes", 30) or 30),
        "emergency_pause_thresholds": dict(getattr(cfg, "emergency_pause_thresholds", {}) or {}),
        "emergency_pause_notify_admin": bool(getattr(cfg, "emergency_pause_notify_admin", True)),
    }


def add_bot_profile(cfg: Config, runtime_manager: "BotRuntimeManager") -> bool:
    """Interactively add another bot profile to the config."""
    was_multi_bot = cfg.is_multi_bot_config()

    if not was_multi_bot:
        print("This will convert the current setup to multi-bot mode and keep the existing bot as the first profile.")
        confirm = input("Continue? (y/n): ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Cancelled.")
            return False
        cfg.bots = [_primary_bot_entry_from_single_config(cfg)]

    print()
    print("Add Another Bot")
    print("-" * 40)

    token = _prompt_bot_token()
    if not token:
        print("Bot creation cancelled.")
        return False

    admin_id = _prompt_admin_chat_id(token, _default_admin_chat_id(cfg))
    if not admin_id:
        print("Bot creation cancelled.")
        return False

    channel = _prompt_channel_username()
    if not channel:
        print("Bot creation cancelled.")
        return False

    suggested_name = _suggest_profile_name(channel, "New Bot")
    while True:
        raw_name = input(f"Profile name (blank uses '{suggested_name}'): ").strip()
        profile_name = raw_name or suggested_name
        profile_key = cfg._sanitize_profile_key(profile_name, "bot")
        existing_keys = {entry["key"] for entry in build_runtime_entries(cfg)}
        if profile_key in existing_keys:
            print("That profile name conflicts with an existing bot. Pick a different name.")
            continue
        break

    subreddits = _prompt_subreddits()
    if not subreddits:
        print("Bot creation cancelled.")
        return False

    spoiler_default = bool(getattr(cfg, "spoiler_posts_enabled", False))
    spoiler_prompt = "Enable spoiler effect for every posted photo/video"
    spoiler_enabled = _prompt_bool(spoiler_prompt, spoiler_default)
    if spoiler_enabled is None:
        spoiler_enabled = spoiler_default

    state_file = os.path.join("states", f"{profile_key}.json")
    new_bot = {
        "name": profile_name,
        "state_file": state_file,
        "bot_token": token,
        "admin_chat_id": admin_id,
        "channels": [{"username": channel, "description": "Main channel"}],
        "subreddits": subreddits,
        "spoiler_posts_enabled": bool(spoiler_enabled),
    }

    cfg.bots.append(new_bot)
    if not cfg.save():
        print("Could not save the updated config.")
        return False

    print(f"Added bot profile '{profile_name}' for {channel}.")

    if runtime_manager.running_count() > 0:
        if not was_multi_bot:
            print("The config is now in multi-bot mode.")
            print("Stop and start posting once so the existing bot reloads under its new profile.")
        else:
            live_cfg = load_config()
            valid, error = live_cfg.validate()
            if not valid:
                print(f"Saved, but could not start the new bot: {error}")
            else:
                started, _ = runtime_manager.start(live_cfg)
                if profile_name in started:
                    print("The new bot was started immediately.")
                else:
                    print("The new bot was saved. Restart posting if you want all profiles reloaded.")
    else:
        print("Start posting to launch the new bot.")

    return True


class BotRuntimeManager:
    """Tracks concurrently running bot threads."""

    def __init__(self) -> None:
        self.runtimes: Dict[str, Dict[str, Any]] = {}

    def _cleanup(self) -> None:
        """Drop finished threads from the runtime map."""
        dead_keys = []
        for key, runtime in self.runtimes.items():
            thread = runtime.get("thread")
            if isinstance(thread, threading.Thread) and not thread.is_alive():
                dead_keys.append(key)

        for key in dead_keys:
            self.runtimes.pop(key, None)

    def running_count(self) -> int:
        """Return the number of currently alive bot threads."""
        self._cleanup()
        return sum(
            1
            for runtime in self.runtimes.values()
            if isinstance(runtime.get("thread"), threading.Thread) and runtime["thread"].is_alive()
        )

    def start(self, cfg: Config) -> Tuple[List[str], List[str]]:
        """Start all configured bot runtimes that are not already running."""
        self._cleanup()
        started: List[str] = []
        already_running: List[str] = []

        for entry in build_runtime_entries(cfg):
            key = entry["key"]
            bot_cfg = entry["config"]
            existing = self.runtimes.get(key)
            if existing and isinstance(existing.get("thread"), threading.Thread):
                if existing["thread"].is_alive():
                    already_running.append(bot_cfg.profile_name)
                    continue

            bot = RedditTelegramBot(config=bot_cfg, state_path=entry["state_path"])
            thread = threading.Thread(
                target=bot.run,
                name=f"bot-{bot_cfg.profile_key}",
                daemon=True,
            )
            self.runtimes[key] = {
                "thread": thread,
                "bot": bot,
                "config": bot_cfg,
                "state_path": entry["state_path"],
            }
            thread.start()
            started.append(bot_cfg.profile_name)

        return started, already_running

    def stop_all(self) -> List[str]:
        """Stop all tracked bot runtimes and return any threads still winding down."""
        self._cleanup()
        if not self.runtimes:
            return []

        for runtime in self.runtimes.values():
            bot = runtime.get("bot")
            if isinstance(bot, RedditTelegramBot):
                bot.stop()

        still_running: List[str] = []
        for runtime in list(self.runtimes.values()):
            thread = runtime.get("thread")
            bot_cfg = runtime.get("config")
            if not isinstance(thread, threading.Thread):
                continue
            thread.join(timeout=6)
            if thread.is_alive():
                profile_name = getattr(bot_cfg, "profile_name", "bot")
                still_running.append(profile_name)

        self._cleanup()
        return still_running


class DashboardRuntimeManager:
    """Tracks the local dashboard server."""

    def __init__(self, runtime_manager: BotRuntimeManager) -> None:
        self.runtime_manager = runtime_manager
        self.server: Optional[LocalDashboardServer] = None

    def is_running(self) -> bool:
        """Return True when the dashboard is running."""
        return bool(self.server and self.server.is_running())

    @property
    def url(self) -> str:
        """Return dashboard URL or an empty string."""
        return self.server.url if self.server else ""

    def start(self, host: str = "127.0.0.1", port: int = 8765) -> str:
        """Start the local dashboard if needed and return its URL."""
        if self.is_running():
            return self.url

        self.server = LocalDashboardServer(
            host,
            port,
            lambda: build_dashboard_snapshot(self.runtime_manager),
            lambda action, payload: handle_dashboard_action(
                self.runtime_manager,
                action,
                payload,
            ),
        )
        self.server.start()
        return self.server.url

    def stop(self) -> None:
        """Stop the dashboard server."""
        if self.server:
            self.server.stop()
        self.server = None


def normalize_subreddit(name: str) -> str:
    """Normalize user-entered subreddit names."""
    value = (name or "").strip()
    if value.lower().startswith("r/"):
        value = value[2:]
    return value.strip()


def format_interval(minutes: int) -> str:
    """Format interval text similar to 'every 2 hours'."""
    if minutes <= 0:
        return "every ? minutes"
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        unit = "hour" if hours == 1 else "hours"
        return f"every {hours} {unit}"
    if minutes >= 60:
        hours_float = minutes / 60.0
        return f"every {hours_float:.1f} hours"
    unit = "minute" if minutes == 1 else "minutes"
    return f"every {minutes} {unit}"


CAPTION_MODE_LABELS = {
    "template": "Custom caption",
    "source": "Copy Reddit title",
    "source_plus_footer": "Reddit title + custom text",
    "source_plus_body": "Source title + body excerpt",
    "source_with_credit": "Source title + credit",
    "credit_only": "Credit only (r/subreddit)",
    "none": "No caption",
    "variants": "Rotate caption variants",
}


def caption_mode_label(mode: str) -> str:
    """Get a friendly label for the current caption mode."""
    return CAPTION_MODE_LABELS.get(mode, CAPTION_MODE_LABELS["template"])


def video_rules_summary(cfg: Config) -> str:
    """Return a compact summary of configured video rules."""
    duration = int(getattr(cfg, "max_video_length_seconds", 0) or 0)
    duration_text = f"{duration}s" if duration > 0 else "unlimited"
    audio = str(getattr(cfg, "video_audio_policy", "allow_silent") or "allow_silent").replace("_", " ")
    orientation = str(getattr(cfg, "video_orientation_rule", "any") or "any")
    convert = "mp4" if getattr(cfg, "video_convert_to_mp4", True) else "original"
    if getattr(cfg, "video_compression_enabled", True):
        compression = f"{int(getattr(cfg, 'video_compression_target_mb', 40) or 40)}MB"
    else:
        compression = "no compression"
    return f"{duration_text} | {orientation} | {audio} | {convert} | {compression}"


def image_quality_summary(cfg: Config) -> str:
    """Return a compact summary of configured image quality rules."""
    if not getattr(cfg, "image_quality_rules_enabled", True):
        return "Off"
    min_width = int(getattr(cfg, "min_image_width", 0) or 0)
    min_height = int(getattr(cfg, "min_image_height", 0) or 0)
    size_text = f">={min_width}x{min_height or 'any'}"
    ratio_min = float(getattr(cfg, "image_aspect_ratio_min", 0.20) or 0.20)
    ratio_max = float(getattr(cfg, "image_aspect_ratio_max", 5.00) or 5.00)
    extras = []
    if getattr(cfg, "image_blur_filter_enabled", False):
        extras.append(f"blur {float(getattr(cfg, 'image_blur_score_min', 35.0) or 35.0):g}+")
    if getattr(cfg, "image_screenshot_filter_enabled", False):
        extras.append("no screenshots")
    if getattr(cfg, "image_text_heavy_filter_enabled", False):
        extras.append(f"text <= {float(getattr(cfg, 'image_text_heavy_max_edge_density', 0.18) or 0.18):.2f}")
    extra_text = " | " + ", ".join(extras) if extras else ""
    return f"{size_text} | ratio {ratio_min:g}-{ratio_max:g}{extra_text}"


def render_menu(
    runtime_manager: BotRuntimeManager,
    dashboard_manager: DashboardRuntimeManager,
    cfg: Config,
    entries: List[Dict[str, Any]],
) -> None:
    """Render the main menu UI."""
    running_count = runtime_manager.running_count()
    bot_count = len(entries)
    paused_count = sum(1 for entry in entries if entry["state"].is_paused())
    pending_count = sum(1 for entry in entries if entry["state"].has_pending())
    subreddit_count = sum(len(entry["config"].subreddits) for entry in entries)
    is_multi_bot = cfg.is_multi_bot_config()
    dashboard_text = dashboard_manager.url if dashboard_manager.is_running() else "Off"

    if running_count > 0 and paused_count == 0:
        status_text = "Running"
        status = colorize(status_text, BOLD, FG_GREEN)
    elif paused_count == bot_count and bot_count > 0:
        status_text = "Paused"
        status = colorize(status_text, BOLD, FG_YELLOW)
    elif running_count > 0 or paused_count > 0:
        status_text = "Mixed"
        status = colorize(status_text, BOLD, FG_YELLOW)
    else:
        status_text = "Stopped"
        status = colorize(status_text, BOLD, FG_RED)

    header_colored = colorize(HEADER, BOLD, FG_MAGENTA)
    tagline_colored = colorize(TAGLINE, DIM, FG_WHITE)

    if is_multi_bot:
        sources_text = f"{bot_count} bots / {subreddit_count} subreddits"
        posting_text = f"{running_count} running / {pending_count} pending"

        caption_modes = {entry["config"].caption_mode for entry in entries}
        if len(caption_modes) == 1 and entries:
            caption_text = caption_mode_label(entries[0]["config"].caption_mode)
        else:
            caption_text = "Per bot"

        daily_limits = {entry["config"].daily_post_limit for entry in entries}
        if len(daily_limits) == 1 and entries:
            limit_value = entries[0]["config"].daily_post_limit
            daily_limit_text = "unlimited" if limit_value <= 0 else f"{limit_value} / day each"
        else:
            daily_limit_text = "Per bot"

        spoiler_flags = {bool(getattr(entry["config"], "spoiler_posts_enabled", False)) for entry in entries}
        if len(spoiler_flags) == 1 and entries:
            spoiler_text = "On" if next(iter(spoiler_flags)) else "Off"
        else:
            spoiler_text = "Mixed"

        reaction_flags = {bool(getattr(entry["config"], "auto_reactions_enabled", True)) for entry in entries}
        if len(reaction_flags) == 1 and entries:
            reactions_text = "On" if next(iter(reaction_flags)) else "Off"
        else:
            reactions_text = "Mixed"

        scoring_flags = {bool(getattr(entry["config"], "smart_scoring_enabled", True)) for entry in entries}
        if len(scoring_flags) == 1 and entries:
            scoring_text = "On" if next(iter(scoring_flags)) else "Off"
        else:
            scoring_text = "Mixed"

        weekly_flags = {bool(getattr(entry["config"], "weekly_schedule_enabled", False)) for entry in entries}
        if len(weekly_flags) == 1 and entries:
            weekly_text = "On" if next(iter(weekly_flags)) else "Off"
        else:
            weekly_text = "Mixed"

        emergency_flags = {bool(getattr(entry["config"], "emergency_pause_enabled", True)) for entry in entries}
        active_emergency_count = sum(1 for entry in entries if entry["state"].get_emergency_pause())
        if active_emergency_count:
            emergency_text = f"{active_emergency_count} active"
        elif len(emergency_flags) == 1 and entries:
            emergency_text = "On" if next(iter(emergency_flags)) else "Off"
        else:
            emergency_text = "Mixed"

        gallery_settings = {
            (
                bool(getattr(entry["config"], "gallery_posts_enabled", True)),
                int(getattr(entry["config"], "min_gallery_items", 2) or 2),
                int(getattr(entry["config"], "max_gallery_items", 6) or 6),
            )
            for entry in entries
        }
        if len(gallery_settings) == 1 and entries:
            gallery_enabled, gallery_min, gallery_max = next(iter(gallery_settings))
            gallery_text = f"On {gallery_min}-{gallery_max}" if gallery_enabled else "Off"
        else:
            gallery_text = "Mixed"

        image_quality_settings = {image_quality_summary(entry["config"]) for entry in entries}
        if len(image_quality_settings) == 1 and entries:
            image_quality_text = next(iter(image_quality_settings))
        else:
            image_quality_text = "Mixed"

        video_settings = {video_rules_summary(entry["config"]) for entry in entries}
        if len(video_settings) == 1 and entries:
            video_text = next(iter(video_settings))
        else:
            video_text = "Mixed"

        domain_flags = {bool(getattr(entry["config"], "domain_downloaders_enabled", True)) for entry in entries}
        if len(domain_flags) == 1 and entries:
            domain_text = "On" if next(iter(domain_flags)) else "Off"
        else:
            domain_text = "Mixed"
    else:
        sources_text = f"{len(cfg.subreddits)} subreddits"
        posting_text = format_interval(cfg.post_interval_minutes)
        caption_text = caption_mode_label(getattr(cfg, "caption_mode", "template"))
        daily_limit = getattr(cfg, "daily_post_limit", 0)
        if daily_limit and daily_limit > 0:
            daily_limit_text = f"{daily_limit} / day"
        else:
            daily_limit_text = "unlimited"
        spoiler_text = "On" if getattr(cfg, "spoiler_posts_enabled", False) else "Off"
        reactions_text = "On" if getattr(cfg, "auto_reactions_enabled", True) else "Off"
        scoring_text = "On" if getattr(cfg, "smart_scoring_enabled", True) else "Off"
        weekly_text = "On" if getattr(cfg, "weekly_schedule_enabled", False) else "Off"
        active_emergency = entries[0]["state"].get_emergency_pause() if entries else None
        if active_emergency:
            emergency_text = f"Paused {active_emergency.get('category', '?')}"
        else:
            emergency_text = "On" if getattr(cfg, "emergency_pause_enabled", True) else "Off"
        gallery_text = (
            f"On {getattr(cfg, 'min_gallery_items', 2)}-{getattr(cfg, 'max_gallery_items', 6)}"
            if getattr(cfg, "gallery_posts_enabled", True)
            else "Off"
        )
        image_quality_text = image_quality_summary(cfg)
        video_text = video_rules_summary(cfg)
        domain_text = "On" if getattr(cfg, "domain_downloaders_enabled", True) else "Off"

    sources_colored = colorize(sources_text, BOLD, FG_CYAN)
    posting_colored = colorize(posting_text, BOLD, FG_CYAN)
    caption_colored = colorize(caption_text, BOLD, FG_CYAN)
    daily_limit_colored = colorize(daily_limit_text, BOLD, FG_CYAN)
    spoiler_colored = colorize(spoiler_text, BOLD, FG_CYAN)
    reactions_colored = colorize(reactions_text, BOLD, FG_CYAN)
    scoring_colored = colorize(scoring_text, BOLD, FG_CYAN)
    weekly_colored = colorize(weekly_text, BOLD, FG_CYAN)
    emergency_colored = colorize(
        emergency_text, BOLD, FG_RED if "Paused" in emergency_text or "active" in emergency_text else FG_CYAN
    )
    dashboard_colored = colorize(dashboard_text, BOLD, FG_CYAN)
    gallery_colored = colorize(gallery_text, BOLD, FG_CYAN)
    image_quality_colored = colorize(image_quality_text, BOLD, FG_CYAN)
    video_colored = colorize(video_text, BOLD, FG_CYAN)
    domain_colored = colorize(domain_text, BOLD, FG_CYAN)

    # Clear interactive terminals without invoking a shell command.
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

    width = 78

    def strip_ansi(text: str) -> str:
        """Remove ANSI escape sequences so padding aligns visually."""
        import re

        return re.sub(r"\x1b\[[0-9;]*m", "", text or "")

    def display_width(text: str) -> int:
        """
        Estimate terminal display width.
        Emojis and wide East Asian characters are treated as width 2.
        """
        import unicodedata

        plain = strip_ansi(text)
        total = 0
        for ch in plain:
            code = ord(ch)
            east_asian = unicodedata.east_asian_width(ch)
            is_wide = east_asian in {"W", "F"}
            is_emoji = 0x1F300 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0xFE00 <= code <= 0xFE0F
            total += 2 if (is_wide or is_emoji) else 1
        return total

    def border(char: str = "-") -> str:
        return "+" + (char * (width - 2))

    def row(label: str, value: str) -> str:
        label_w = 14
        value_w = width - label_w - 7
        pad = max(0, value_w - display_width(value))
        return f"| {label:<{label_w}} | {value}{' ' * pad} "

    def full(text: str = "") -> str:
        pad_w = width - 4
        pad = max(0, pad_w - display_width(text))
        return f"| {text}{' ' * pad} "

    print(header_colored)
    print(tagline_colored)
    print()
    print(colorize(border("="), BOLD, FG_MAGENTA))
    print(colorize(full("MAIN MENU"), BOLD, FG_MAGENTA))
    print(colorize(border("-"), DIM, FG_WHITE))
    print(colorize(row("Automation", status), FG_WHITE))
    print(colorize(row("Sources", sources_colored), FG_WHITE))
    print(colorize(row("Posting", posting_colored), FG_WHITE))
    print(colorize(row("Caption", caption_colored), FG_WHITE))
    print(colorize(row("Scoring", scoring_colored), FG_WHITE))
    print(colorize(row("Weekly", weekly_colored), FG_WHITE))
    print(colorize(row("Spoiler", spoiler_colored), FG_WHITE))
    print(colorize(row("Reactions", reactions_colored), FG_WHITE))
    print(colorize(row("Daily limit", daily_limit_colored), FG_WHITE))
    print(colorize(row("Emergency", emergency_colored), FG_WHITE))
    print(colorize(row("Gallery", gallery_colored), FG_WHITE))
    print(colorize(row("Image", image_quality_colored), FG_WHITE))
    print(colorize(row("Video", video_colored), FG_WHITE))
    print(colorize(row("Domains", domain_colored), FG_WHITE))
    print(colorize(row("Dashboard", dashboard_colored), FG_WHITE))
    print(colorize(border("-"), DIM, FG_WHITE))
    print(colorize(full("1 ] Start Posting"), BOLD, FG_GREEN))
    print(colorize(full("2 ] Stop Posting"), BOLD, FG_GREEN))
    print(colorize(full(""), DIM, FG_WHITE))

    print(colorize(full("3 ] Add Subreddit"), BOLD, FG_CYAN))
    print(colorize(full("4 ] Remove Subreddit"), BOLD, FG_CYAN))
    print(colorize(full(""), DIM, FG_WHITE))

    print(colorize(full("5 ] List Subreddits"), BOLD, FG_CYAN))
    print(colorize(full("6 ] Change Posting Interval"), BOLD, FG_YELLOW))
    print(colorize(full("7 ] Posts Per Day Limit"), BOLD, FG_YELLOW))
    print(colorize(full(""), DIM, FG_WHITE))

    print(colorize(full("8 ] Statistics"), BOLD, FG_MAGENTA))
    print(colorize(full("9 ] Caption Options"), BOLD, FG_YELLOW))
    print(colorize(full(""), DIM, FG_WHITE))

    print(colorize(full("10 ] Delete Saved Data"), BOLD, FG_RED))
    print(colorize(full(""), DIM, FG_WHITE))

    print(colorize(full("11 ] Settings"), BOLD, FG_CYAN))
    print(colorize(full("12 ] Add Another Bot"), BOLD, FG_CYAN))
    print(colorize(full("13 ] Spoiler Options"), BOLD, FG_YELLOW))
    print(colorize(full("14 ] Reaction Options"), BOLD, FG_YELLOW))
    print(colorize(full("15 ] Queue"), BOLD, FG_CYAN))
    print(colorize(full("16 ] Subreddit Rules"), BOLD, FG_CYAN))
    print(colorize(full("17 ] Scoring Options"), BOLD, FG_YELLOW))
    print(colorize(full("18 ] Weekly Schedule"), BOLD, FG_YELLOW))
    print(colorize(full("19 ] Analytics"), BOLD, FG_MAGENTA))
    print(colorize(full("20 ] Duplicate Detection"), BOLD, FG_YELLOW))
    print(colorize(full("22 ] Emergency Pause"), BOLD, FG_YELLOW))
    print(colorize(full("23 ] Config Backup"), BOLD, FG_CYAN))
    print(colorize(full("24 ] Local Dashboard"), BOLD, FG_CYAN))
    print(colorize(full("25 ] Gallery Support"), BOLD, FG_CYAN))
    print(colorize(full("26 ] Domain Downloaders"), BOLD, FG_CYAN))
    print(colorize(full("27 ] Video Rules"), BOLD, FG_CYAN))
    print(colorize(full("28 ] Image Quality"), BOLD, FG_CYAN))
    print(colorize(full("29 ] Health Check"), BOLD, FG_MAGENTA))
    print(colorize(full("30 ] Error Logs"), BOLD, FG_MAGENTA))
    print(colorize(full("31 ] Auto Recovery"), BOLD, FG_MAGENTA))
    print(colorize(full(""), DIM, FG_WHITE))

    if is_multi_bot:
        print(
            colorize(
                full("Multi-bot mode: profile-aware options let you pick the bot to edit."),
                DIM,
                FG_WHITE,
            )
        )
        print(colorize(full(""), DIM, FG_WHITE))

    print(colorize(full("0 ] Close"), BOLD, FG_RED))
    print(colorize(border("="), BOLD, FG_MAGENTA))
    print()


def list_subreddits(cfg: Config) -> None:
    """Print configured subreddits."""
    if cfg.is_multi_bot_config():
        entries = build_runtime_entries(cfg)
        if not entries:
            print("No bot profiles configured.")
            return

        print("Configured bot profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            interval = format_interval(bot_cfg.post_interval_minutes)
            print(f"  {idx}. {bot_cfg.profile_name} -> {channel} | {len(bot_cfg.subreddits)} subs | {interval}")
            if bot_cfg.subreddits:
                preview_parts = []
                for sub in bot_cfg.subreddits[:8]:
                    marker = "*" if bot_cfg.get_subreddit_rule(sub) else ""
                    preview_parts.append(f"r/{sub}{marker}")
                preview = ", ".join(preview_parts)
                if len(bot_cfg.subreddits) > 8:
                    preview += ", ..."
                print(f"     {preview}")
                if bot_cfg.subreddit_rules:
                    print("     * has per-subreddit rules")
        return

    if not cfg.subreddits:
        print("No subreddits configured.")
        return
    print("Configured subreddits:")
    for idx, sub in enumerate(cfg.subreddits, 1):
        marker = " *" if cfg.get_subreddit_rule(sub) else ""
        print(f"  {idx}. r/{sub}{marker}")
    if cfg.subreddit_rules:
        print("  * has per-subreddit rules")


def add_subreddit(cfg: Config) -> None:
    """Prompt user to add subreddits in a loop."""
    if cfg.is_multi_bot_config():
        _print_multi_bot_edit_hint()
        return

    print("Add subreddits (press Enter on empty input to return).")
    while True:
        raw = input("Enter subreddit to add (without r/): ").strip()
        if not raw:
            print("Returning to main menu.")
            return

        value = normalize_subreddit(raw)
        if not value:
            print("No subreddit entered.")
            continue
        if value in cfg.subreddits:
            print(f"r/{value} is already in the list.")
            continue

        cfg.subreddits.append(value)
        cfg.save()
        print(f"Added r/{value}.")
        print("Changes apply the next time posting starts.")


def remove_subreddit(cfg: Config) -> None:
    """Prompt user to remove subreddits in a loop."""
    if cfg.is_multi_bot_config():
        _print_multi_bot_edit_hint()
        return

    if not cfg.subreddits:
        print("No subreddits to remove.")
        return

    print("Remove subreddits (press Enter on empty input to return).")
    while True:
        list_subreddits(cfg)
        raw = input("Enter number or subreddit name to remove: ").strip()
        if not raw:
            print("Returning to main menu.")
            return

        removed = None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(cfg.subreddits):
                removed = cfg.subreddits.pop(idx - 1)
        else:
            name = normalize_subreddit(raw)
            if name in cfg.subreddits:
                cfg.subreddits.remove(name)
                removed = name

        if not removed:
            print("Could not find that subreddit.")
            continue

        cfg.save()
        print(f"Removed r/{removed}.")
        print("Changes apply the next time posting starts.")

        if not cfg.subreddits:
            print("No subreddits remaining. Returning to main menu.")
            return


def change_interval(cfg: Config) -> None:
    """Prompt user to change the posting interval in minutes."""
    if cfg.is_multi_bot_config():
        _print_multi_bot_edit_hint()
        return

    raw = input("Enter new posting interval in minutes (e.g., 120): ").strip()
    if not raw:
        print("No value entered.")
        return
    try:
        minutes = int(raw)
    except ValueError:
        print("Please enter a whole number of minutes.")
        return

    if minutes < 1:
        print("Interval must be at least 1 minute.")
        return

    cfg.post_interval_minutes = minutes
    cfg.save()
    print(f"Posting interval updated to {format_interval(minutes)}.")
    print("Changes apply the next time posting starts.")


def change_daily_limit(cfg: Config) -> None:
    """Prompt user to change the daily post limit."""
    if cfg.is_multi_bot_config():
        _print_multi_bot_edit_hint()
        return

    current = getattr(cfg, "daily_post_limit", 0)
    if current and current > 0:
        print(f"Current daily limit: {current} posts/day")
    else:
        print("Current daily limit: unlimited")

    raw = input("Enter max posts per day (0 = unlimited): ").strip()
    if not raw:
        print("No value entered.")
        return

    try:
        limit = int(raw)
    except ValueError:
        print("Please enter a whole number (0 or greater).")
        return

    if limit < 0:
        print("Daily limit cannot be negative.")
        return

    cfg.daily_post_limit = limit
    cfg.save()
    if limit == 0:
        print("Daily post limit disabled (unlimited).")
    else:
        print(f"Daily post limit set to {limit} posts/day.")
    print("Changes apply the next time posting starts.")


def _prompt_int(
    label: str,
    current: int,
    *,
    min_value: Optional[int] = None,
) -> Optional[int]:
    """Prompt for an integer value; blank input cancels."""
    raw = input(f"{label} (current: {current}): ").strip()
    if not raw:
        print("No change.")
        return None
    try:
        value = int(raw)
    except ValueError:
        print("Please enter a whole number.")
        return None
    if min_value is not None and value < min_value:
        print(f"Value must be at least {min_value}.")
        return None
    return value


def _prompt_float(
    label: str,
    current: float,
    *,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> Optional[float]:
    """Prompt for a float value; blank input cancels."""
    raw = input(f"{label} (current: {current:g}): ").strip()
    if not raw:
        print("No change.")
        return None
    try:
        value = float(raw)
    except ValueError:
        print("Please enter a number.")
        return None
    if min_value is not None and value < min_value:
        print(f"Value must be at least {min_value:g}.")
        return None
    if max_value is not None and value > max_value:
        print(f"Value must be at most {max_value:g}.")
        return None
    return value


def _prompt_bool(label: str, current: bool) -> Optional[bool]:
    """Prompt for a boolean value; blank input cancels."""
    current_text = "on" if current else "off"
    raw = input(f"{label} (current: {current_text}) [y/n]: ").strip().lower()
    if not raw:
        print("No change.")
        return None
    if raw in {"y", "yes", "1", "true", "on"}:
        return True
    if raw in {"n", "no", "0", "false", "off"}:
        return False
    print("Please answer y or n.")
    return None


def _prompt_list(label: str, current: list[str]) -> Optional[list[str]]:
    """Prompt for a comma-separated list; blank input cancels."""
    current_text = ", ".join(current) if current else "(empty)"
    raw = input(f"{label} (current: {current_text}) [comma-separated]: ").strip()
    if not raw:
        print("No change.")
        return None
    items = [item.strip() for item in raw.split(",")]
    return [item for item in items if item]


def _prompt_text(label: str, current: str) -> Optional[str]:
    """Prompt for a text value; blank input cancels."""
    raw = input(f"{label} (current: {current}): ").strip()
    if not raw:
        print("No change.")
        return None
    return raw


def spoiler_options(cfg: Config, runtime_manager: BotRuntimeManager) -> None:
    """Configure spoiler posting behavior."""
    if not cfg.is_multi_bot_config():
        current = bool(getattr(cfg, "spoiler_posts_enabled", False))
        value = _prompt_bool("Enable spoiler effect on posted photos/videos", current)
        if value is None:
            return
        cfg.spoiler_posts_enabled = value
        if cfg.save():
            print(f"Spoiler effect is now {'on' if value else 'off'}.")
            for runtime in runtime_manager.runtimes.values():
                runtime_cfg = runtime.get("config")
                if isinstance(runtime_cfg, Config):
                    runtime_cfg.spoiler_posts_enabled = value
        else:
            print("Could not save spoiler setting.")
        return

    print("Spoiler effect options:")
    entries = build_runtime_entries(cfg)
    for idx, entry in enumerate(entries, 1):
        bot_cfg = entry["config"]
        spoiler_text = "On" if getattr(bot_cfg, "spoiler_posts_enabled", False) else "Off"
        channel = bot_cfg.get_default_channel() or "(no channel)"
        print(f"{idx}. {bot_cfg.profile_name} -> {channel} [{spoiler_text}]")
    print("A. Apply to all bots")
    print("0. Back")
    print()

    choice = input("Select a bot profile: ").strip().lower()
    if choice in {"0", "back", "b", ""}:
        return

    target_indexes: List[int] = []
    if choice in {"a", "all"}:
        target_indexes = list(range(len(cfg.bots)))
        current = all(bool(bot.get("spoiler_posts_enabled", False)) for bot in cfg.bots)
    elif choice.isdigit():
        idx = int(choice)
        if not (1 <= idx <= len(cfg.bots)):
            print("Unknown bot profile.")
            return
        target_indexes = [idx - 1]
        current = bool(cfg.bots[idx - 1].get("spoiler_posts_enabled", False))
    else:
        print("Unknown choice.")
        return

    value = _prompt_bool("Enable spoiler effect on posted photos/videos", current)
    if value is None:
        return

    touched_keys: set[str] = set()
    for index in target_indexes:
        cfg.bots[index]["spoiler_posts_enabled"] = value
        if 0 <= index < len(entries):
            touched_keys.add(entries[index]["key"])

    if not cfg.save():
        print("Could not save spoiler settings.")
        return

    for key, runtime in runtime_manager.runtimes.items():
        if key not in touched_keys:
            continue
        runtime_cfg = runtime.get("config")
        if isinstance(runtime_cfg, Config):
            runtime_cfg.spoiler_posts_enabled = value

    if len(target_indexes) == 1:
        print(f"Spoiler effect updated for {cfg.bots[target_indexes[0]].get('name', 'bot')}.")
    else:
        print(f"Spoiler effect updated for {len(target_indexes)} bots.")


def reaction_options(cfg: Config, runtime_manager: BotRuntimeManager) -> None:
    """Configure automatic post reactions for one or more bots."""
    if not cfg.is_multi_bot_config():
        current = bool(getattr(cfg, "auto_reactions_enabled", True))
        value = _prompt_bool("Enable automatic reactions on posted messages", current)
        if value is None:
            return
        cfg.auto_reactions_enabled = value
        if cfg.save():
            print(f"Automatic reactions are now {'on' if value else 'off'}.")
            _sync_runtime_fields(cfg, REACTION_FIELD_NAMES, runtime_manager)
        else:
            print("Could not save reaction setting.")
        return

    print("Reaction options:")
    entries = build_runtime_entries(cfg)
    for idx, entry in enumerate(entries, 1):
        bot_cfg = entry["config"]
        reaction_text = "On" if getattr(bot_cfg, "auto_reactions_enabled", True) else "Off"
        channel = bot_cfg.get_default_channel() or "(no channel)"
        print(f"{idx}. {bot_cfg.profile_name} -> {channel} [{reaction_text}]")
    print("A. Apply to all bots")
    print("0. Back")
    print()

    choice = input("Select a bot profile: ").strip().lower()
    if choice in {"0", "back", "b", ""}:
        return

    target_indexes: List[int] = []
    if choice in {"a", "all"}:
        target_indexes = list(range(len(cfg.bots)))
        current = all(bool(bot.get("auto_reactions_enabled", True)) for bot in cfg.bots)
    elif choice.isdigit():
        idx = int(choice)
        if not (1 <= idx <= len(cfg.bots)):
            print("Unknown bot profile.")
            return
        target_indexes = [idx - 1]
        current = bool(cfg.bots[idx - 1].get("auto_reactions_enabled", True))
    else:
        print("Unknown choice.")
        return

    value = _prompt_bool("Enable automatic reactions on posted messages", current)
    if value is None:
        return

    touched_keys: set[str] = set()
    for index in target_indexes:
        cfg.bots[index]["auto_reactions_enabled"] = value
        if 0 <= index < len(entries):
            touched_keys.add(entries[index]["key"])

    if not cfg.save():
        print("Could not save reaction settings.")
        return

    for key, runtime in runtime_manager.runtimes.items():
        if key not in touched_keys:
            continue
        runtime_cfg = runtime.get("config")
        if isinstance(runtime_cfg, Config):
            runtime_cfg.auto_reactions_enabled = value

    if len(target_indexes) == 1:
        print(f"Automatic reactions updated for {cfg.bots[target_indexes[0]].get('name', 'bot')}.")
    else:
        print(f"Automatic reactions updated for {len(target_indexes)} bots.")


def _runtime_state_for_entry(
    runtime_manager: BotRuntimeManager,
    entry: Dict[str, Any],
) -> StateManager:
    """Return the live StateManager when a bot is running, otherwise load from disk."""
    runtime = runtime_manager.runtimes.get(entry["key"])
    if runtime:
        bot = runtime.get("bot")
        if isinstance(bot, RedditTelegramBot):
            return bot.state
    return load_state(entry["state_path"])


def _dashboard_runtime_running(runtime_manager: BotRuntimeManager, key: str) -> bool:
    """Return True when a bot runtime thread is alive."""
    runtime_manager._cleanup()
    runtime = runtime_manager.runtimes.get(key)
    thread = runtime.get("thread") if runtime else None
    return isinstance(thread, threading.Thread) and thread.is_alive()


def _dashboard_trim_text(value: Any, limit: int = 160) -> str:
    """Return compact text for dashboard JSON."""
    text = str(value or "").strip()
    return text[: limit - 3] + "..." if len(text) > limit else text


def _dashboard_queue_items(state: StateManager, limit: int = 20) -> List[Dict[str, Any]]:
    """Return queue items for the dashboard."""
    items: List[Dict[str, Any]] = []
    for item in state.get_post_queue()[:limit]:
        items.append(
            {
                "id": str(item.get("id") or ""),
                "subreddit": str(item.get("subreddit") or "?"),
                "type": str(item.get("type") or "?"),
                "title": _dashboard_trim_text(item.get("title"), 220),
                "queued_at_utc": item.get("queued_at_utc"),
            }
        )
    return items


def _dashboard_recent_posts(state: StateManager, limit: int = 8) -> List[Dict[str, Any]]:
    """Return recent posted history, newest first."""
    history = []
    for item in reversed(state.get_history(limit)):
        history.append(
            {
                "id": str(item.get("id") or ""),
                "subreddit": str(item.get("subreddit") or "?"),
                "type": str(item.get("type") or "?"),
                "timestamp": str(item.get("timestamp") or ""),
            }
        )
    return history


def build_dashboard_snapshot(runtime_manager: BotRuntimeManager) -> Dict[str, Any]:
    """Build the JSON payload used by the local dashboard."""
    cfg = load_config()
    entries = build_runtime_entries(cfg)
    profiles: List[Dict[str, Any]] = []
    total_queue = 0
    total_pending = 0
    running_count = 0

    for entry in entries:
        bot_cfg = entry["config"]
        state = _runtime_state_for_entry(runtime_manager, entry)
        is_running = _dashboard_runtime_running(runtime_manager, entry["key"])
        if is_running:
            running_count += 1
        pending = state.get_pending() if state.has_pending() else None
        queue_count = state.get_post_queue_count()
        total_queue += queue_count
        total_pending += 1 if pending else 0
        emergency_pause = state.get_emergency_pause()
        if emergency_pause:
            status = "emergency"
        elif state.is_paused():
            status = "paused"
        elif is_running:
            status = "running"
        else:
            status = "stopped"

        daily_limit = int(getattr(bot_cfg, "daily_post_limit", 0) or 0)
        profiles.append(
            {
                "key": entry["key"],
                "name": bot_cfg.profile_name,
                "status": status,
                "running": is_running,
                "state_path": os.path.relpath(entry["state_path"], APP_BASE_DIR),
                "channel": bot_cfg.get_default_channel() or "",
                "subreddits": list(bot_cfg.subreddits),
                "pending": {
                    "id": str(pending.get("id") or ""),
                    "subreddit": str(pending.get("subreddit") or "?"),
                    "type": str(pending.get("type") or "?"),
                    "title": _dashboard_trim_text(pending.get("title"), 220),
                    "deadline_utc": pending.get("deadline_utc"),
                }
                if pending
                else None,
                "queue_count": queue_count,
                "queue": _dashboard_queue_items(state),
                "recent_posts": _dashboard_recent_posts(state),
                "stats": state.get_stats(),
                "last_error": state.get_last_error(),
                "emergency_pause": emergency_pause,
                "settings": {
                    "post_interval_minutes": int(getattr(bot_cfg, "post_interval_minutes", 0) or 0),
                    "daily_post_limit": daily_limit,
                    "daily_post_limit_text": "unlimited" if daily_limit <= 0 else str(daily_limit),
                    "timezone": getattr(bot_cfg, "timezone", "UTC"),
                    "caption_mode": getattr(bot_cfg, "caption_mode", "template"),
                    "smart_scoring_enabled": bool(getattr(bot_cfg, "smart_scoring_enabled", True)),
                    "weekly_schedule_enabled": bool(getattr(bot_cfg, "weekly_schedule_enabled", False)),
                    "emergency_pause_enabled": bool(getattr(bot_cfg, "emergency_pause_enabled", True)),
                    "gallery_posts_enabled": bool(getattr(bot_cfg, "gallery_posts_enabled", True)),
                    "min_gallery_items": int(getattr(bot_cfg, "min_gallery_items", 2) or 2),
                    "max_gallery_items": int(getattr(bot_cfg, "max_gallery_items", 6) or 6),
                    "image_quality": image_quality_summary(bot_cfg),
                    "image_quality_rules_enabled": bool(getattr(bot_cfg, "image_quality_rules_enabled", True)),
                    "min_image_width": int(getattr(bot_cfg, "min_image_width", 800) or 0),
                    "min_image_height": int(getattr(bot_cfg, "min_image_height", 0) or 0),
                    "image_aspect_ratio_min": float(getattr(bot_cfg, "image_aspect_ratio_min", 0.20) or 0.20),
                    "image_aspect_ratio_max": float(getattr(bot_cfg, "image_aspect_ratio_max", 5.00) or 5.00),
                    "video_rules": video_rules_summary(bot_cfg),
                    "max_video_length_seconds": int(getattr(bot_cfg, "max_video_length_seconds", 0) or 0),
                    "video_audio_policy": getattr(bot_cfg, "video_audio_policy", "allow_silent"),
                    "video_orientation_rule": getattr(bot_cfg, "video_orientation_rule", "any"),
                    "video_convert_to_mp4": bool(getattr(bot_cfg, "video_convert_to_mp4", True)),
                    "video_compression_enabled": bool(getattr(bot_cfg, "video_compression_enabled", True)),
                    "video_compression_target_mb": int(getattr(bot_cfg, "video_compression_target_mb", 40) or 40),
                    "domain_downloaders_enabled": bool(getattr(bot_cfg, "domain_downloaders_enabled", True)),
                    "imgur_album_downloads_enabled": bool(getattr(bot_cfg, "imgur_album_downloads_enabled", True)),
                    "html_media_resolver_enabled": bool(getattr(bot_cfg, "html_media_resolver_enabled", True)),
                },
            }
        )

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "profile_count": len(profiles),
            "running_count": running_count,
            "pending_count": total_pending,
            "queue_count": total_queue,
            "multi_bot": cfg.is_multi_bot_config(),
        },
        "profiles": profiles,
        "logs": get_log_history(80),
    }


def _dashboard_entry_by_key(cfg: Config, key: str) -> Optional[Dict[str, Any]]:
    """Find a runtime entry by profile key."""
    for entry in build_runtime_entries(cfg):
        if entry["key"] == key:
            return entry
    return None


def handle_dashboard_action(
    runtime_manager: BotRuntimeManager,
    action: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply a local dashboard action."""
    cfg = load_config()
    profile_key = str(payload.get("profile_key") or "").strip()
    entry = _dashboard_entry_by_key(cfg, profile_key)
    if not entry:
        return {"ok": False, "error": "Unknown profile"}

    state = _runtime_state_for_entry(runtime_manager, entry)
    action_key = str(action or "").strip().lower()

    if action_key == "pause":
        state.set_paused(True)
        state.save()
        return {"ok": True, "message": "Paused"}

    if action_key == "resume":
        state.set_paused(False)
        state.save()
        return {"ok": True, "message": "Resumed"}

    if action_key == "queue_clear":
        removed = state.clear_post_queue()
        state.save()
        return {"ok": True, "message": f"Cleared {removed} item(s)"}

    if action_key in {"queue_remove", "queue_up", "queue_down"}:
        try:
            index = int(payload.get("index", 0) or 0)
        except (TypeError, ValueError):
            index = 0
        if index < 1:
            return {"ok": False, "error": "Invalid queue index"}
        if action_key == "queue_remove":
            removed = state.remove_queued_post(index)
            if not removed:
                return {"ok": False, "error": "Queue item not found"}
            state.save()
            return {"ok": True, "message": "Queue item removed"}
        direction = -1 if action_key == "queue_up" else 1
        if not state.move_queued_post(index, direction):
            return {"ok": False, "error": "Queue item cannot move"}
        state.save()
        return {"ok": True, "message": "Queue item moved"}

    return {"ok": False, "error": "Unknown action"}


def _format_queue_item(index: int, item: Dict[str, Any]) -> str:
    """Format a queue item for the CLI."""
    title = str(item.get("title") or "").strip() or "(untitled)"
    if len(title) > 72:
        title = title[:69] + "..."
    return f"{index}. r/{item.get('subreddit', '?')} [{item.get('type', '?')}] {title}"


def _queue_menu_impl(label: str, state: StateManager) -> None:
    """Manage approved post queue from the CLI."""
    while True:
        queue = state.get_post_queue()
        print(f"Queue for {label}:")
        if queue:
            for index, item in enumerate(queue[:20], 1):
                print(f"  {_format_queue_item(index, item)}")
            if len(queue) > 20:
                print(f"  ...and {len(queue) - 20} more.")
        else:
            print("  Queue is empty.")

        print()
        print("1. Remove queued item")
        print("2. Move item up")
        print("3. Move item down")
        print("4. Clear queue")
        print("5. Post next queued item on the next tick")
        print("0. Back")
        print()

        choice = input("Select a queue option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice == "1":
            raw = input("Item number to remove: ").strip()
            if not raw.isdigit():
                print("Please enter an item number.")
                print()
                continue
            removed = state.remove_queued_post(int(raw))
            if removed:
                state.save()
                print(f"Removed r/{removed.get('subreddit', '?')} from the queue.")
            else:
                print("Queue item not found.")
        elif choice in {"2", "3"}:
            raw = input("Item number to move: ").strip()
            if not raw.isdigit():
                print("Please enter an item number.")
                print()
                continue
            direction = -1 if choice == "2" else 1
            if state.move_queued_post(int(raw), direction):
                state.save()
                print("Queue item moved.")
            else:
                print("Queue item cannot be moved that way.")
        elif choice == "4":
            confirm = input("Type CLEAR to clear the queue: ").strip()
            if confirm == "CLEAR":
                count = state.clear_post_queue()
                state.save()
                print(f"Cleared {count} queued item(s).")
            else:
                print("Clear cancelled.")
        elif choice == "5":
            if not state.has_queued_posts():
                print("Queue is empty.")
            elif state.has_pending():
                print("Approve, queue, or skip the current pending preview first.")
            else:
                state.set_last_tick(None)
                state.save()
                print("The next queued item will post as soon as the scheduler ticks.")
        else:
            print("Unknown queue option.")

        print()


def queue_options(cfg: Config, runtime_manager: BotRuntimeManager) -> None:
    """Manage approved post queues for one or more bot profiles."""
    entries = load_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    if len(entries) == 1:
        entry = entries[0]
        state = _runtime_state_for_entry(runtime_manager, entry)
        _queue_menu_impl(entry["config"].profile_name, state)
        return

    while True:
        print("Queue Profiles:")
        for idx, entry in enumerate(entries, 1):
            state = _runtime_state_for_entry(runtime_manager, entry)
            channel = entry["config"].get_default_channel() or "(no channel)"
            print(f"{idx}. {entry['config'].profile_name} -> {channel} | {state.get_post_queue_count()} queued")
        print("0. Back")
        print()

        choice = input("Select a bot profile: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        entry = entries[index]
        state = _runtime_state_for_entry(runtime_manager, entry)
        _queue_menu_impl(entry["config"].profile_name, state)
        entries = load_runtime_entries(cfg)


def _format_recent_analytics_record(index: int, record: Dict[str, Any]) -> str:
    """Format one recent analytics row for the CLI."""
    title = str(record.get("title") or "").strip() or "(untitled)"
    if len(title) > 64:
        title = title[:61] + "..."

    posted_at = str(record.get("posted_at_utc") or "").replace("T", " ")[:16] or "unknown time"
    metrics = []
    for key, label in (
        ("views", "views"),
        ("reaction_total", "reactions"),
        ("forwards", "forwards"),
    ):
        value = record.get(key)
        if isinstance(value, int):
            metrics.append(f"{value} {label}")

    metric_text = ", ".join(metrics) if metrics else "metrics pending"
    return (
        f"{index}. {posted_at} UTC | r/{record.get('subreddit', '?')} "
        f"[{record.get('type', '?')}] | {metric_text} | {title}"
    )


def _analytics_menu_impl(label: str, state: StateManager) -> None:
    """Show performance analytics from the CLI."""
    days = 7
    while True:
        print(f"Analytics for {label}:")
        print(state.build_analytics_report(days=days, limit=5))
        print()
        print("1. Last 7 days")
        print("2. Last 30 days")
        print("3. Last 90 days")
        print("4. Recent tracked posts")
        print("0. Back")
        print()

        choice = input("Select an analytics option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if choice == "1":
            days = 7
        elif choice == "2":
            days = 30
        elif choice == "3":
            days = 90
        elif choice == "4":
            records = state.get_post_analytics(limit=10)
            if not records:
                print("No tracked posts yet.")
            else:
                print("Recent tracked posts:")
                for index, record in enumerate(records, 1):
                    print(f"  {_format_recent_analytics_record(index, record)}")
            print()
            input("Press Enter to continue...")
            print()
        else:
            print("Unknown analytics option.")
            print()


def analytics_options(cfg: Config, runtime_manager: BotRuntimeManager) -> None:
    """Show performance analytics for one or more bot profiles."""
    entries = load_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    if len(entries) == 1:
        entry = entries[0]
        state = _runtime_state_for_entry(runtime_manager, entry)
        _analytics_menu_impl(entry["config"].profile_name, state)
        return

    while True:
        print("Analytics Profiles:")
        print("A. All profiles")
        for idx, entry in enumerate(entries, 1):
            state = _runtime_state_for_entry(runtime_manager, entry)
            channel = entry["config"].get_default_channel() or "(no channel)"
            tracked = len(state.get_post_analytics(limit=0))
            print(f"{idx}. {entry['config'].profile_name} -> {channel} | {tracked} tracked post(s)")
        print("0. Back")
        print()

        choice = input("Select a bot profile: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if choice in {"a", "all"}:
            for entry in entries:
                state = _runtime_state_for_entry(runtime_manager, entry)
                print(f"=== {entry['config'].profile_name} ===")
                print(state.build_analytics_report(days=7, limit=5))
                print()
            input("Press Enter to continue...")
            print()
            entries = load_runtime_entries(cfg)
            continue
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        entry = entries[index]
        state = _runtime_state_for_entry(runtime_manager, entry)
        _analytics_menu_impl(entry["config"].profile_name, state)
        entries = load_runtime_entries(cfg)


def _print_duplicate_detection_summary(cfg: Config, state: Optional[StateManager] = None) -> None:
    """Print duplicate detection settings and available state counts."""
    title_enabled = bool(getattr(cfg, "duplicate_title_similarity_enabled", True))
    title_threshold = float(getattr(cfg, "duplicate_title_similarity_threshold", 0.88) or 0.88)
    title_limit = int(getattr(cfg, "duplicate_title_similarity_history_limit", 500) or 500)
    crosspost_enabled = bool(getattr(cfg, "duplicate_crosspost_blocking", True))
    author_enabled = bool(getattr(cfg, "author_cooldown_enabled", False))
    author_hours = int(getattr(cfg, "author_cooldown_hours", 24) or 0)

    print("Duplicate Detection:")
    print("  URL normalization: On")
    print(f"  Crosspost blocking: {'On' if crosspost_enabled else 'Off'}")
    print(
        f"  Title similarity: {'On' if title_enabled else 'Off'} (threshold {title_threshold:.2f}, last {title_limit})"
    )
    print(f"  Author cooldown: {'On' if author_enabled else 'Off'} ({author_hours}h)")
    if state:
        title_count = len(state.get_title_signatures(state.get_blocked_signatures()))
        author_count = len(state.state.get("author_history", []) or [])
        signature_count = len(state.get_blocked_signatures())
        print(f"  Stored title signatures: {title_count}")
        print(f"  Stored author entries: {author_count}")
        print(f"  Blocked content signatures: {signature_count}")
    print()


def _duplicate_detection_menu_impl(
    cfg: Config,
    state: StateManager,
    save_callback,
) -> None:
    """Configure duplicate detection for one bot profile."""
    while True:
        _print_duplicate_detection_summary(cfg, state)
        print("1. Toggle crosspost blocking")
        print("2. Toggle title similarity checks")
        print("3. Set title similarity threshold")
        print("4. Set title similarity history limit")
        print("5. Toggle author cooldown")
        print("6. Set author cooldown hours")
        print("0. Back")
        print()

        choice = input("Select duplicate detection option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            cfg.duplicate_crosspost_blocking = not bool(getattr(cfg, "duplicate_crosspost_blocking", True))
            updated = True
            print(f"Crosspost blocking {'enabled' if cfg.duplicate_crosspost_blocking else 'disabled'}.")
        elif choice == "2":
            cfg.duplicate_title_similarity_enabled = not bool(getattr(cfg, "duplicate_title_similarity_enabled", True))
            updated = True
            print(f"Title similarity checks {'enabled' if cfg.duplicate_title_similarity_enabled else 'disabled'}.")
        elif choice == "3":
            value = _prompt_float(
                "Title similarity threshold",
                float(getattr(cfg, "duplicate_title_similarity_threshold", 0.88) or 0.88),
                min_value=0.5,
                max_value=1.0,
            )
            if value is not None:
                cfg.duplicate_title_similarity_threshold = value
                updated = True
        elif choice == "4":
            value = _prompt_int(
                "Title similarity history limit",
                int(getattr(cfg, "duplicate_title_similarity_history_limit", 500) or 500),
                min_value=1,
            )
            if value is not None:
                cfg.duplicate_title_similarity_history_limit = value
                updated = True
        elif choice == "5":
            cfg.author_cooldown_enabled = not bool(getattr(cfg, "author_cooldown_enabled", False))
            updated = True
            print(f"Author cooldown {'enabled' if cfg.author_cooldown_enabled else 'disabled'}.")
        elif choice == "6":
            value = _prompt_int(
                "Author cooldown hours",
                int(getattr(cfg, "author_cooldown_hours", 24) or 0),
                min_value=0,
            )
            if value is not None:
                cfg.author_cooldown_hours = value
                updated = True
        else:
            print("Unknown duplicate detection option.")

        if updated:
            if save_callback():
                print("Saved duplicate detection settings.")
            else:
                print("Could not save duplicate detection settings.")
        print()


def duplicate_detection_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure duplicate detection from the CLI."""
    if not cfg.is_multi_bot_config():
        state = load_state()

        def save_single_dedupe() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, DUPLICATE_DETECTION_FIELD_NAMES, runtime_manager)
            return saved

        _duplicate_detection_menu_impl(cfg, state, save_single_dedupe)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Duplicate Detection Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            title_state = "title on" if getattr(bot_cfg, "duplicate_title_similarity_enabled", True) else "title off"
            author_state = "author on" if getattr(bot_cfg, "author_cooldown_enabled", False) else "author off"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {title_state}, {author_state}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        state = load_state(entries[index]["state_path"])
        _duplicate_detection_menu_impl(
            selected_cfg,
            state,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                DUPLICATE_DETECTION_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _build_health_report_for_entry(
    entry: Dict[str, Any],
    state: StateManager,
    runtime_manager: Optional[BotRuntimeManager],
    *,
    include_live_checks: bool = True,
) -> str:
    """Build a health report, preferring live handlers when the bot is running."""
    bot_cfg = entry["config"]
    bot = None
    if runtime_manager:
        runtime = runtime_manager.runtimes.get(entry["key"])
        if runtime:
            candidate = runtime.get("bot")
            if isinstance(candidate, RedditTelegramBot):
                bot = candidate

    if bot and bot.commands:
        return bot.commands.build_health_report(include_live_checks=include_live_checks)

    live_state = bot.state if bot else state
    scheduler = bot.scheduler if bot and bot.scheduler else Scheduler(bot_cfg)
    handler = CommandHandler(
        bot_cfg,
        live_state,
        scheduler,
        traffic_service=bot.traffic if bot else None,
        telegram_handler=bot.telegram if bot else None,
        reddit_handler=bot.reddit if bot else None,
        media_handler=bot.media if bot else None,
    )
    return handler.build_health_report(include_live_checks=include_live_checks)


def _print_health_report_for_entry(
    entry: Dict[str, Any],
    runtime_manager: Optional[BotRuntimeManager],
    *,
    include_live_checks: bool = True,
) -> None:
    """Print one profile health report."""
    state = _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
    bot_cfg = entry["config"]
    print(f"Health Check for {bot_cfg.profile_name}:")
    print(
        _build_health_report_for_entry(
            entry,
            state,
            runtime_manager,
            include_live_checks=include_live_checks,
        )
    )


def health_check_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Run health checks from the CLI."""
    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    include_live_checks = True
    if not cfg.is_multi_bot_config():
        _print_health_report_for_entry(
            entries[0],
            runtime_manager,
            include_live_checks=include_live_checks,
        )
        print()
        input("Press Enter to continue...")
        return

    while True:
        print("Health Check Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            state = (
                _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
            )
            channel = bot_cfg.get_default_channel() or "(no channel)"
            queue_count = state.get_post_queue_count()
            pending_text = "pending" if state.has_pending() else "no pending"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {queue_count} queued, {pending_text}")
        print("a. Check all profiles")
        print("q. Quick local check for all profiles")
        print("0. Back")
        print()

        choice = input("Select a bot profile to check: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice in {"a", "all"}:
            for index, entry in enumerate(entries, 1):
                if index > 1:
                    print()
                    print("-" * 60)
                _print_health_report_for_entry(
                    entry,
                    runtime_manager,
                    include_live_checks=True,
                )
            print()
            input("Press Enter to continue...")
            print()
            continue

        if choice in {"q", "quick", "local"}:
            for index, entry in enumerate(entries, 1):
                if index > 1:
                    print()
                    print("-" * 60)
                _print_health_report_for_entry(
                    entry,
                    runtime_manager,
                    include_live_checks=False,
                )
            print()
            input("Press Enter to continue...")
            print()
            continue

        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        _print_health_report_for_entry(
            entries[index],
            runtime_manager,
            include_live_checks=True,
        )
        print()
        input("Press Enter to continue...")
        print()


def _build_error_report_for_entry(
    entry: Dict[str, Any],
    state: StateManager,
    runtime_manager: Optional[BotRuntimeManager],
    *,
    limit: int = 10,
) -> str:
    """Build an error report, preferring live handlers when the bot is running."""
    bot_cfg = entry["config"]
    bot = None
    if runtime_manager:
        runtime = runtime_manager.runtimes.get(entry["key"])
        if runtime:
            candidate = runtime.get("bot")
            if isinstance(candidate, RedditTelegramBot):
                bot = candidate

    if bot and bot.commands:
        return bot.commands.build_error_report(limit=limit)

    live_state = bot.state if bot else state
    scheduler = bot.scheduler if bot and bot.scheduler else Scheduler(bot_cfg)
    handler = CommandHandler(
        bot_cfg,
        live_state,
        scheduler,
        traffic_service=bot.traffic if bot else None,
        telegram_handler=bot.telegram if bot else None,
        reddit_handler=bot.reddit if bot else None,
        media_handler=bot.media if bot else None,
    )
    return handler.build_error_report(limit=limit)


def _print_error_report_for_entry(
    entry: Dict[str, Any],
    runtime_manager: Optional[BotRuntimeManager],
    *,
    limit: int = 10,
) -> None:
    """Print one profile error report."""
    state = _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
    bot_cfg = entry["config"]
    print(f"Error Logs for {bot_cfg.profile_name}:")
    print(
        _build_error_report_for_entry(
            entry,
            state,
            runtime_manager,
            limit=limit,
        )
    )


def error_log_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """View recent runtime errors from the CLI."""
    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    if not cfg.is_multi_bot_config():
        _print_error_report_for_entry(entries[0], runtime_manager, limit=10)
        print()
        input("Press Enter to continue...")
        return

    while True:
        print("Error Log Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            state = (
                _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
            )
            channel = bot_cfg.get_default_channel() or "(no channel)"
            recent = state.get_recent_errors(50)
            count_text = "50+" if len(recent) >= 50 else str(len(recent))
            last_text = ""
            if recent:
                message = str(recent[0].get("message") or "").strip()
                if len(message) > 52:
                    message = message[:49] + "..."
                last_text = f" | last: {message}"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {count_text} stored error(s){last_text}")
        print("a. Show all profiles")
        print("0. Back")
        print()

        choice = input("Select a bot profile to view: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice in {"a", "all"}:
            for index, entry in enumerate(entries, 1):
                if index > 1:
                    print()
                    print("-" * 60)
                _print_error_report_for_entry(entry, runtime_manager, limit=10)
            print()
            input("Press Enter to continue...")
            print()
            continue

        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        _print_error_report_for_entry(entries[index], runtime_manager, limit=10)
        print()
        input("Press Enter to continue...")
        print()


def _build_auto_recovery_report_for_entry(
    entry: Dict[str, Any],
    state: StateManager,
    runtime_manager: Optional[BotRuntimeManager],
    *,
    limit: int = 8,
) -> str:
    """Build an auto-recovery report, preferring live handlers when available."""
    bot_cfg = entry["config"]
    bot = None
    if runtime_manager:
        runtime = runtime_manager.runtimes.get(entry["key"])
        if runtime:
            candidate = runtime.get("bot")
            if isinstance(candidate, RedditTelegramBot):
                bot = candidate

    if bot and bot.commands:
        return bot.commands.build_auto_recovery_report(limit=limit)

    live_state = bot.state if bot else state
    scheduler = bot.scheduler if bot and bot.scheduler else Scheduler(bot_cfg)
    handler = CommandHandler(
        bot_cfg,
        live_state,
        scheduler,
        traffic_service=bot.traffic if bot else None,
        telegram_handler=bot.telegram if bot else None,
        reddit_handler=bot.reddit if bot else None,
        media_handler=bot.media if bot else None,
    )
    return handler.build_auto_recovery_report(limit=limit)


def _print_auto_recovery_summary(cfg: Config, state: StateManager) -> None:
    """Print concise auto-recovery settings and recent state."""
    window = int(getattr(cfg, "auto_recovery_notify_window_minutes", 30) or 30)
    threshold = int(getattr(cfg, "auto_recovery_notify_threshold", 3) or 3)
    failures = state.get_recovery_failure_count(window)
    recent_count = len(state.get_recovery_events(100))
    print(f"  Status: {'On' if getattr(cfg, 'auto_recovery_enabled', True) else 'Off'}")
    print(
        "  Upload retries: "
        f"{int(getattr(cfg, 'auto_recovery_upload_retries', 2) or 0)} "
        f"with {int(getattr(cfg, 'auto_recovery_retry_delay_seconds', 5) or 0)}s delay"
    )
    print(
        "  Compression fallback: "
        f"{'On' if getattr(cfg, 'auto_recovery_compress_on_retry', True) else 'Off'} "
        f"(video {int(getattr(cfg, 'auto_recovery_video_target_mb', 30) or 30)} MB, "
        f"image {int(getattr(cfg, 'auto_recovery_image_target_mb', 8) or 8)} MB)"
    )
    print(f"  Stuck pending skip: {int(getattr(cfg, 'auto_recovery_stuck_pending_minutes', 90) or 90)} min")
    print(
        "  Admin alert: "
        f"{threshold} failure(s) / {window} min, "
        f"{int(getattr(cfg, 'auto_recovery_notify_cooldown_minutes', 30) or 30)} min cooldown"
    )
    print(f"  Recent recovery failures: {failures}/{threshold}")
    print(f"  Stored recovery events: {recent_count}")
    print()


def _auto_recovery_menu_impl(
    label: str,
    cfg: Config,
    state: StateManager,
    save_callback,
    report_callback,
) -> None:
    """Configure auto-recovery settings for one bot profile."""
    while True:
        print(f"Auto Recovery for {label}:")
        _print_auto_recovery_summary(cfg, state)
        print("1. Toggle auto-recovery")
        print("2. Set upload retry count")
        print("3. Set retry delay seconds")
        print("4. Toggle compression fallback")
        print("5. Set retry video target MB")
        print("6. Set retry image target MB")
        print("7. Set stuck pending minutes")
        print("8. Set admin alert threshold")
        print("9. Set admin alert window minutes")
        print("10. Set admin alert cooldown minutes")
        print("11. Show recovery report")
        print("12. Clear recovery event history")
        print("0. Back")
        print()

        choice = input("Select auto-recovery option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            cfg.auto_recovery_enabled = not bool(getattr(cfg, "auto_recovery_enabled", True))
            updated = True
            print(f"Auto-recovery {'enabled' if cfg.auto_recovery_enabled else 'disabled'}.")
        elif choice == "2":
            value = _prompt_int(
                "Upload retry count",
                int(getattr(cfg, "auto_recovery_upload_retries", 2) or 0),
                min_value=0,
            )
            if value is not None:
                cfg.auto_recovery_upload_retries = min(5, value)
                updated = True
        elif choice == "3":
            value = _prompt_int(
                "Retry delay seconds",
                int(getattr(cfg, "auto_recovery_retry_delay_seconds", 5) or 0),
                min_value=0,
            )
            if value is not None:
                cfg.auto_recovery_retry_delay_seconds = min(60, value)
                updated = True
        elif choice == "4":
            cfg.auto_recovery_compress_on_retry = not bool(getattr(cfg, "auto_recovery_compress_on_retry", True))
            updated = True
            print(f"Compression fallback {'enabled' if cfg.auto_recovery_compress_on_retry else 'disabled'}.")
        elif choice == "5":
            value = _prompt_int(
                "Retry video target MB",
                int(getattr(cfg, "auto_recovery_video_target_mb", 30) or 30),
                min_value=1,
            )
            if value is not None:
                max_download = max(1, int(getattr(cfg, "max_download_mb", 45) or 45))
                cfg.auto_recovery_video_target_mb = min(max_download, value)
                updated = True
        elif choice == "6":
            value = _prompt_int(
                "Retry image target MB",
                int(getattr(cfg, "auto_recovery_image_target_mb", 8) or 8),
                min_value=1,
            )
            if value is not None:
                cfg.auto_recovery_image_target_mb = min(10, value)
                updated = True
        elif choice == "7":
            value = _prompt_int(
                "Stuck pending minutes",
                int(getattr(cfg, "auto_recovery_stuck_pending_minutes", 90) or 90),
                min_value=1,
            )
            if value is not None:
                cfg.auto_recovery_stuck_pending_minutes = value
                updated = True
        elif choice == "8":
            value = _prompt_int(
                "Admin alert threshold",
                int(getattr(cfg, "auto_recovery_notify_threshold", 3) or 3),
                min_value=1,
            )
            if value is not None:
                cfg.auto_recovery_notify_threshold = value
                updated = True
        elif choice == "9":
            value = _prompt_int(
                "Admin alert window minutes",
                int(getattr(cfg, "auto_recovery_notify_window_minutes", 30) or 30),
                min_value=1,
            )
            if value is not None:
                cfg.auto_recovery_notify_window_minutes = value
                updated = True
        elif choice == "10":
            value = _prompt_int(
                "Admin alert cooldown minutes",
                int(getattr(cfg, "auto_recovery_notify_cooldown_minutes", 30) or 30),
                min_value=1,
            )
            if value is not None:
                cfg.auto_recovery_notify_cooldown_minutes = value
                updated = True
        elif choice == "11":
            print(report_callback())
            print()
            input("Press Enter to continue...")
        elif choice == "12":
            cleared = state.clear_recovery_events()
            if state.save():
                print(f"Cleared {cleared} auto-recovery event(s).")
            else:
                print("Could not save state after clearing recovery events.")
        else:
            print("Unknown auto-recovery option.")

        if updated:
            if save_callback():
                print("Saved auto-recovery settings.")
            else:
                print("Could not save auto-recovery settings.")
        print()


def auto_recovery_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure auto-recovery from the CLI."""
    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    if not cfg.is_multi_bot_config():
        entry = entries[0]
        state = _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])

        def save_single_recovery() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, AUTO_RECOVERY_FIELD_NAMES, runtime_manager)
            return saved

        _auto_recovery_menu_impl(
            cfg.profile_name,
            cfg,
            state,
            save_single_recovery,
            lambda: _build_auto_recovery_report_for_entry(
                entry,
                state,
                runtime_manager,
            ),
        )
        return

    while True:
        print("Auto Recovery Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            state = (
                _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
            )
            channel = bot_cfg.get_default_channel() or "(no channel)"
            status = "on" if getattr(bot_cfg, "auto_recovery_enabled", True) else "off"
            recent_failures = state.get_recovery_failure_count(
                int(getattr(bot_cfg, "auto_recovery_notify_window_minutes", 30) or 30)
            )
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {status}, {recent_failures} recent failure(s)")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        entry = entries[index]
        selected_cfg = entry["config"]
        state = _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
        _auto_recovery_menu_impl(
            selected_cfg.profile_name,
            selected_cfg,
            state,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                AUTO_RECOVERY_FIELD_NAMES,
                runtime_manager,
            ),
            lambda entry=entry, state=state: _build_auto_recovery_report_for_entry(
                entry,
                state,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _build_emergency_pause_report(cfg: Config, state: StateManager) -> str:
    """Build an emergency pause report for CLI display."""
    scheduler = Scheduler(cfg)
    handler = CommandHandler(cfg, state, scheduler)
    return handler.build_emergency_pause_report()


def _emergency_thresholds(cfg: Config) -> Dict[str, int]:
    """Return emergency pause thresholds with defaults filled in."""
    defaults = {
        "reddit": 5,
        "telegram": 3,
        "download": 6,
        "empty_feed": 4,
    }
    raw = getattr(cfg, "emergency_pause_thresholds", {}) or {}
    thresholds = dict(defaults)
    if isinstance(raw, dict):
        for category in thresholds:
            try:
                thresholds[category] = max(0, int(raw.get(category, thresholds[category]) or 0))
            except (TypeError, ValueError):
                pass
    return thresholds


def _print_emergency_pause_summary(cfg: Config, state: StateManager) -> None:
    """Print emergency pause settings and current rolling counts."""
    enabled = bool(getattr(cfg, "emergency_pause_enabled", True))
    window = max(1, int(getattr(cfg, "emergency_pause_window_minutes", 30) or 30))
    thresholds = _emergency_thresholds(cfg)
    counts = state.get_emergency_failure_counts(window)
    active = state.get_emergency_pause()

    print("Emergency Pause:")
    print(f"  Status: {'On' if enabled else 'Off'}")
    print(f"  Window: {window} min")
    print(f"  Admin alert: {'On' if getattr(cfg, 'emergency_pause_notify_admin', True) else 'Off'}")
    if active:
        print(
            f"  Active pause: {active.get('category', 'unknown')} {active.get('count', 0)}/{active.get('threshold', 0)}"
        )
        reason = str(active.get("reason") or "").strip()
        if reason:
            print(f"  Reason: {reason[:100]}")
    else:
        print("  Active pause: none")
    print("  Rolling counts:")
    for category in ("reddit", "telegram", "download", "empty_feed"):
        threshold = thresholds.get(category, 0)
        threshold_text = "off" if threshold <= 0 else str(threshold)
        print(f"    {category}: {counts.get(category, 0)}/{threshold_text}")
    print()


def _set_emergency_threshold(cfg: Config, category: str) -> bool:
    """Prompt and update one emergency pause threshold."""
    thresholds = _emergency_thresholds(cfg)
    current = thresholds.get(category, 0)
    value = _prompt_int(
        f"{category} failure threshold (0 disables category)",
        current,
        min_value=0,
    )
    if value is None:
        return False
    thresholds[category] = value
    cfg.emergency_pause_thresholds = thresholds
    return True


def _emergency_pause_menu_impl(
    label: str,
    cfg: Config,
    state: StateManager,
    save_callback,
) -> None:
    """Configure emergency pause settings for one bot profile."""
    while True:
        print(f"Emergency Pause for {label}:")
        _print_emergency_pause_summary(cfg, state)
        print("1. Toggle emergency pause")
        print("2. Set rolling window minutes")
        print("3. Set Reddit failure threshold")
        print("4. Set Telegram failure threshold")
        print("5. Set download failure threshold")
        print("6. Set empty-feed failure threshold")
        print("7. Toggle admin alert")
        print("8. Show failure report")
        print("9. Clear failure history and emergency pause")
        print("0. Back")
        print()

        choice = input("Select emergency pause option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            cfg.emergency_pause_enabled = not bool(getattr(cfg, "emergency_pause_enabled", True))
            updated = True
            print(f"Emergency pause {'enabled' if cfg.emergency_pause_enabled else 'disabled'}.")
        elif choice == "2":
            value = _prompt_int(
                "Rolling failure window minutes",
                int(getattr(cfg, "emergency_pause_window_minutes", 30) or 30),
                min_value=1,
            )
            if value is not None:
                cfg.emergency_pause_window_minutes = value
                updated = True
        elif choice == "3":
            updated = _set_emergency_threshold(cfg, "reddit")
        elif choice == "4":
            updated = _set_emergency_threshold(cfg, "telegram")
        elif choice == "5":
            updated = _set_emergency_threshold(cfg, "download")
        elif choice == "6":
            updated = _set_emergency_threshold(cfg, "empty_feed")
        elif choice == "7":
            cfg.emergency_pause_notify_admin = not bool(getattr(cfg, "emergency_pause_notify_admin", True))
            updated = True
            print(f"Admin alert {'enabled' if cfg.emergency_pause_notify_admin else 'disabled'}.")
        elif choice == "8":
            print(_build_emergency_pause_report(cfg, state))
            print()
            input("Press Enter to continue...")
        elif choice == "9":
            state.clear_emergency_pause()
            state.clear_emergency_failures()
            state.set_paused(False)
            if state.save():
                print("Emergency pause history cleared and posting resumed.")
            else:
                print("Could not save state after clearing emergency pause history.")
        else:
            print("Unknown emergency pause option.")

        if updated:
            if save_callback():
                print("Saved emergency pause settings.")
            else:
                print("Could not save emergency pause settings.")
        print()


def emergency_pause_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure emergency pause rules from the CLI."""
    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    if not cfg.is_multi_bot_config():
        entry = entries[0]
        state = _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])

        def save_single_emergency() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, EMERGENCY_PAUSE_FIELD_NAMES, runtime_manager)
            return saved

        _emergency_pause_menu_impl(cfg.profile_name, cfg, state, save_single_emergency)
        return

    while True:
        print("Emergency Pause Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            state = (
                _runtime_state_for_entry(runtime_manager, entry) if runtime_manager else load_state(entry["state_path"])
            )
            active = state.get_emergency_pause()
            status = "active" if active else ("on" if getattr(bot_cfg, "emergency_pause_enabled", True) else "off")
            channel = bot_cfg.get_default_channel() or "(no channel)"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {status}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        state = (
            _runtime_state_for_entry(runtime_manager, entries[index])
            if runtime_manager
            else load_state(entries[index]["state_path"])
        )
        _emergency_pause_menu_impl(
            selected_cfg.profile_name,
            selected_cfg,
            state,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                EMERGENCY_PAUSE_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _print_backup_list() -> None:
    """Print available local backup archives."""
    backups = _list_config_backups()
    if not backups:
        print("No backup archives found.")
        return
    print("Available backups:")
    for index, path in enumerate(backups, 1):
        print(f"  {index}. {_describe_config_backup(path)}")


def _choose_backup_archive() -> Optional[str]:
    """Prompt for a backup archive from the local backup folder."""
    backups = _list_config_backups()
    if not backups:
        print("No backup archives found.")
        return None

    _print_backup_list()
    print("0. Cancel")
    print()

    raw = input("Backup number to restore: ").strip().lower()
    if raw in {"0", "cancel", "c", ""}:
        print("Restore cancelled.")
        return None
    if not raw.isdigit():
        print("Please enter a backup number.")
        return None

    index = int(raw) - 1
    if index < 0 or index >= len(backups):
        print("Unknown backup number.")
        return None
    return backups[index]


def _restore_backup_from_cli(
    cfg: Config,
    runtime_manager: BotRuntimeManager,
) -> bool:
    """Restore a selected backup archive from the CLI."""
    archive_path = _choose_backup_archive()
    if not archive_path:
        return False

    print()
    print("Restore will overwrite config.json and any state files included in the backup.")
    confirm = input("Type RESTORE to continue: ").strip()
    if confirm != "RESTORE":
        print("Confirmation mismatch. Restore cancelled.")
        return False

    if runtime_manager.running_count() > 0:
        print("Stopping running bots before restore...")
        still_running = runtime_manager.stop_all()
        if still_running:
            print("Some bots are still winding down:")
            for name in still_running:
                print(f"  - {name}")

    try:
        safety_path, safety_files = _create_config_backup_archive(
            cfg,
            label="pre-restore",
            include_state=True,
        )
        print(f"Safety backup created: {os.path.relpath(safety_path, APP_BASE_DIR)} ({len(safety_files)} file(s))")
    except Exception as exc:
        print(f"Could not create safety backup: {exc}")
        confirm_no_backup = input("Continue without a safety backup? (y/n): ").strip().lower()
        if confirm_no_backup not in {"y", "yes"}:
            print("Restore cancelled.")
            return False

    try:
        restored = _restore_config_backup_archive(archive_path)
    except Exception as exc:
        print(f"Restore failed: {exc}")
        return False

    print(f"Restored {len(restored)} file(s):")
    for name in restored:
        print(f"  - {name}")
    print("Configuration will be reloaded when you return to the main menu.")
    return True


def _duplicate_profile_from_cli(
    cfg: Config,
    runtime_manager: BotRuntimeManager,
) -> bool:
    """Prompt for and duplicate a bot profile."""
    if runtime_manager.running_count() > 0:
        print("Profile duplication changes the runtime config.")
        print("Stop and start posting after duplicating so all bot threads reload cleanly.")
        print()

    source_entries = build_runtime_entries(cfg)
    if not source_entries:
        print("No bot profiles available to duplicate.")
        return False

    print("Profiles:")
    for idx, entry in enumerate(source_entries, 1):
        bot_cfg = entry["config"]
        print(f"  {idx}. {bot_cfg.profile_name} -> {bot_cfg.get_default_channel() or '(no channel)'}")
    print("0. Cancel")
    print()

    raw = input("Source profile number: ").strip().lower()
    if raw in {"0", "cancel", "c", ""}:
        print("Duplicate cancelled.")
        return False
    if not raw.isdigit():
        print("Please enter a source profile number.")
        return False

    source_index = int(raw) - 1
    if source_index < 0 or source_index >= len(source_entries):
        print("Unknown source profile.")
        return False

    default_name = f"{source_entries[source_index]['config'].profile_name} Copy"
    new_name = input(f"New profile name (blank uses '{default_name}'): ").strip() or default_name

    try:
        backup_path, backup_files = _create_config_backup_archive(
            cfg,
            label="pre-duplicate",
            include_state=True,
        )
        print(f"Safety backup created: {os.path.relpath(backup_path, APP_BASE_DIR)} ({len(backup_files)} file(s))")
    except Exception as exc:
        print(f"Could not create safety backup: {exc}")

    success, message = _duplicate_profile_in_config(cfg, source_index, new_name)
    if not success:
        print(message)
        return False

    print(f"Duplicated profile '{message}'.")
    print("The new profile starts with a fresh state file.")
    print("Edit its bot token, channel, or subreddits before running if it should be a separate bot.")
    return True


def config_backup_options(cfg: Config, runtime_manager: BotRuntimeManager) -> None:
    """Manage config backups, restores, exports, and profile duplication."""
    while True:
        backups = _list_config_backups()
        print("Config Backup and Restore:")
        print(f"  Config path: {CONFIG_PATH}")
        print(f"  Backup folder: {CONFIG_BACKUP_DIR}")
        print(f"  Backup archives: {len(backups)}")
        print()
        print("1. Create full backup")
        print("2. Restore from backup")
        print("3. Export redacted config")
        print("4. Duplicate bot profile")
        print("5. List backups")
        print("0. Back")
        print()

        choice = input("Select backup option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice == "1":
            label = input("Backup label (blank uses manual): ").strip() or "manual"
            try:
                path, files = _create_config_backup_archive(
                    cfg,
                    label=label,
                    include_state=True,
                )
                print(f"Backup created: {os.path.relpath(path, APP_BASE_DIR)} ({len(files)} file(s))")
            except Exception as exc:
                print(f"Could not create backup: {exc}")
        elif choice == "2":
            _restore_backup_from_cli(cfg, runtime_manager)
        elif choice == "3":
            try:
                export_path = _export_redacted_config()
                print(f"Redacted export created: {os.path.relpath(export_path, APP_BASE_DIR)}")
                print("Bot tokens and admin chat IDs were replaced with REDACTED.")
            except Exception as exc:
                print(f"Could not export config: {exc}")
        elif choice == "4":
            _duplicate_profile_from_cli(cfg, runtime_manager)
        elif choice == "5":
            _print_backup_list()
        else:
            print("Unknown backup option.")

        print()
        input("Press Enter to continue...")
        print()


def dashboard_options(dashboard_manager: DashboardRuntimeManager) -> None:
    """Start, stop, or open the local dashboard."""
    while True:
        print("Local Dashboard:")
        if dashboard_manager.is_running():
            print("  Status: Running")
            print(f"  URL: {dashboard_manager.url}")
        else:
            print("  Status: Stopped")
        print()
        print("1. Start dashboard")
        print("2. Start dashboard on another port")
        print("3. Open dashboard in browser")
        print("4. Stop dashboard")
        print("0. Back")
        print()

        choice = input("Select dashboard option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice == "1":
            try:
                url = dashboard_manager.start()
                print(f"Dashboard running at {url}")
            except OSError as exc:
                print(f"Could not start dashboard: {exc}")
                print("Try another port.")
        elif choice == "2":
            port = _prompt_int("Dashboard port", 8765, min_value=1)
            if port is None:
                continue
            if dashboard_manager.is_running():
                dashboard_manager.stop()
            try:
                url = dashboard_manager.start(port=port)
                print(f"Dashboard running at {url}")
            except OSError as exc:
                print(f"Could not start dashboard: {exc}")
        elif choice == "3":
            if not dashboard_manager.is_running():
                try:
                    dashboard_manager.start()
                except OSError as exc:
                    print(f"Could not start dashboard: {exc}")
                    continue
            print(f"Opening {dashboard_manager.url}")
            webbrowser.open(dashboard_manager.url)
        elif choice == "4":
            if dashboard_manager.is_running():
                dashboard_manager.stop()
                print("Dashboard stopped.")
            else:
                print("Dashboard is not running.")
        else:
            print("Unknown dashboard option.")

        print()
        input("Press Enter to continue...")
        print()


def _gallery_menu_impl(label: str, cfg: Config, save_callback) -> None:
    """Configure Reddit gallery posting for one concrete bot profile."""
    while True:
        enabled = bool(getattr(cfg, "gallery_posts_enabled", True))
        min_items = int(getattr(cfg, "min_gallery_items", 2) or 2)
        max_items = int(getattr(cfg, "max_gallery_items", 6) or 6)

        print(f"Gallery Support for {label}:")
        print(f"  Status: {'On' if enabled else 'Off'}")
        print(f"  Min items: {min_items}")
        print(f"  Max items: {max_items} (Telegram media groups allow up to 10)")
        print()
        print("1. Toggle gallery posts")
        print("2. Set minimum gallery items")
        print("3. Set maximum gallery items")
        print("4. Edit subreddit media-type rules")
        print("0. Back")
        print()

        choice = input("Select gallery option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False

        if choice == "1":
            value = _prompt_bool("Enable Reddit gallery posts", enabled)
            if value is not None:
                cfg.gallery_posts_enabled = value
                updated = True
        elif choice == "2":
            value = _prompt_int("Minimum gallery items", min_items, min_value=2)
            if value is not None:
                cfg.min_gallery_items = min(10, value)
                if cfg.max_gallery_items < cfg.min_gallery_items:
                    cfg.max_gallery_items = cfg.min_gallery_items
                updated = True
        elif choice == "3":
            value = _prompt_int("Maximum gallery items", max_items, min_value=2)
            if value is not None:
                cfg.max_gallery_items = min(10, value)
                if cfg.min_gallery_items > cfg.max_gallery_items:
                    cfg.min_gallery_items = cfg.max_gallery_items
                updated = True
        elif choice == "4":
            print("Use media_type 'gallery' in subreddit rules to allow only gallery posts for a source.")
            print()
            _subreddit_rules_menu_impl(cfg, save_callback)
            continue
        else:
            print("Unknown gallery option.")

        if updated:
            if save_callback():
                print("Saved gallery settings.")
            else:
                print("Could not save gallery settings.")
        print()


def gallery_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure Reddit gallery support from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_gallery() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(
                    cfg,
                    GALLERY_FIELD_NAMES + SUBREDDIT_RULE_FIELD_NAMES,
                    runtime_manager,
                )
            return saved

        _gallery_menu_impl(cfg.profile_name, cfg, save_single_gallery)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Gallery Support Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            status = "on" if getattr(bot_cfg, "gallery_posts_enabled", True) else "off"
            min_items = getattr(bot_cfg, "min_gallery_items", 2)
            max_items = getattr(bot_cfg, "max_gallery_items", 6)
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {status}, {min_items}-{max_items} items")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _gallery_menu_impl(
            selected_cfg.profile_name,
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                GALLERY_FIELD_NAMES + SUBREDDIT_RULE_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _prompt_video_audio_policy(current: str) -> Optional[str]:
    """Prompt for the video audio policy."""
    current = str(current or "allow_silent")
    print("Audio policies:")
    print("  allow_silent  - allow videos with or without audio")
    print("  prefer_audio  - try to keep audio, but allow silent videos")
    print("  require_audio - reject videos without audio")
    value = input(f"Video audio policy (current: {current}, blank keep): ").strip()
    if not value:
        return None
    return Config._normalize_video_audio_policy(Config(), value)


def _prompt_video_orientation_rule(current: str) -> Optional[str]:
    """Prompt for the video orientation rule."""
    current = str(current or "any")
    print("Orientation rules: any, portrait, landscape, square")
    value = input(f"Video orientation rule (current: {current}, blank keep): ").strip()
    if not value:
        return None
    return Config._normalize_video_orientation_rule(Config(), value)


def _video_rules_menu_impl(label: str, cfg: Config, save_callback) -> None:
    """Configure video validation and processing for one bot profile."""
    while True:
        max_duration = int(getattr(cfg, "max_video_length_seconds", 0) or 0)
        audio_policy = str(getattr(cfg, "video_audio_policy", "allow_silent") or "allow_silent")
        orientation_rule = str(getattr(cfg, "video_orientation_rule", "any") or "any")
        convert_to_mp4 = bool(getattr(cfg, "video_convert_to_mp4", True))
        compression_enabled = bool(getattr(cfg, "video_compression_enabled", True))
        target_mb = int(getattr(cfg, "video_compression_target_mb", 40) or 40)
        max_download_mb = int(getattr(cfg, "max_download_mb", 45) or 45)

        print(f"Video Rules for {label}:")
        print(f"  Max duration: {max_duration}s" if max_duration > 0 else "  Max duration: unlimited")
        print(f"  Audio policy: {audio_policy}")
        print(f"  Orientation: {orientation_rule}")
        print(f"  Convert to MP4: {'On' if convert_to_mp4 else 'Off'}")
        print(f"  Compression: {'On' if compression_enabled else 'Off'}")
        print(f"  Compression target: {target_mb} MB (download max: {max_download_mb} MB)")
        print()
        print("1. Set max duration")
        print("2. Set audio policy")
        print("3. Set orientation rule")
        print("4. Toggle MP4 conversion")
        print("5. Toggle compression")
        print("6. Set compression target")
        print("0. Back")
        print()

        choice = input("Select video rule option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            value = _prompt_int("Max video duration seconds (0 = unlimited)", max_duration, min_value=0)
            if value is not None:
                cfg.max_video_length_seconds = value
                updated = True
        elif choice == "2":
            value = _prompt_video_audio_policy(audio_policy)
            if value is not None:
                cfg.video_audio_policy = value
                updated = True
        elif choice == "3":
            value = _prompt_video_orientation_rule(orientation_rule)
            if value is not None:
                cfg.video_orientation_rule = value
                updated = True
        elif choice == "4":
            value = _prompt_bool("Convert videos to MP4", convert_to_mp4)
            if value is not None:
                cfg.video_convert_to_mp4 = value
                updated = True
        elif choice == "5":
            value = _prompt_bool("Compress videos over target size", compression_enabled)
            if value is not None:
                cfg.video_compression_enabled = value
                updated = True
        elif choice == "6":
            value = _prompt_int("Video compression target MB", target_mb, min_value=1)
            if value is not None:
                cfg.video_compression_target_mb = min(max_download_mb, max(1, value))
                updated = True
        else:
            print("Unknown video rule option.")

        if updated:
            if save_callback():
                print("Saved video rules.")
            else:
                print("Could not save video rules.")
        print()


def video_rule_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure video validation and processing rules from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_video_rules() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, VIDEO_RULE_FIELD_NAMES, runtime_manager)
            return saved

        _video_rules_menu_impl(cfg.profile_name, cfg, save_single_video_rules)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Video Rules Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {video_rules_summary(bot_cfg)}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _video_rules_menu_impl(
            selected_cfg.profile_name,
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                VIDEO_RULE_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _image_quality_menu_impl(label: str, cfg: Config, save_callback) -> None:
    """Configure image quality filters for one bot profile."""
    while True:
        enabled = bool(getattr(cfg, "image_quality_rules_enabled", True))
        min_width = int(getattr(cfg, "min_image_width", 800) or 0)
        min_height = int(getattr(cfg, "min_image_height", 0) or 0)
        ratio_min = float(getattr(cfg, "image_aspect_ratio_min", 0.20) or 0.20)
        ratio_max = float(getattr(cfg, "image_aspect_ratio_max", 5.00) or 5.00)
        blur_enabled = bool(getattr(cfg, "image_blur_filter_enabled", False))
        blur_min = float(getattr(cfg, "image_blur_score_min", 35.0) or 35.0)
        screenshot_enabled = bool(getattr(cfg, "image_screenshot_filter_enabled", False))
        text_enabled = bool(getattr(cfg, "image_text_heavy_filter_enabled", False))
        edge_limit = float(getattr(cfg, "image_text_heavy_max_edge_density", 0.18) or 0.18)
        height_text = f"{min_height}px" if min_height else "disabled"

        print(f"Image Quality for {label}:")
        print(f"  Rules: {'On' if enabled else 'Off'}")
        print(f"  Minimum size: {min_width}px wide, height {height_text}")
        print(f"  Aspect ratio: {ratio_min:g} - {ratio_max:g}")
        print(f"  Blur filter: {'On' if blur_enabled else 'Off'} (minimum score {blur_min:g})")
        print(f"  Screenshot filter: {'On' if screenshot_enabled else 'Off'}")
        print(f"  Text-heavy filter: {'On' if text_enabled else 'Off'} (edge density <= {edge_limit:g})")
        print()
        print("1. Toggle image quality rules")
        print("2. Set minimum width")
        print("3. Set minimum height")
        print("4. Set minimum aspect ratio")
        print("5. Set maximum aspect ratio")
        print("6. Toggle blur filter")
        print("7. Set minimum blur score")
        print("8. Toggle screenshot filter")
        print("9. Toggle text-heavy filter")
        print("10. Set text-heavy edge limit")
        print("0. Back")
        print()

        choice = input("Select image quality option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            value = _prompt_bool("Enable image quality rules", enabled)
            if value is not None:
                cfg.image_quality_rules_enabled = value
                updated = True
        elif choice == "2":
            value = _prompt_int("Minimum image width px", min_width, min_value=0)
            if value is not None:
                cfg.min_image_width = value
                updated = True
        elif choice == "3":
            value = _prompt_int("Minimum image height px (0 = disabled)", min_height, min_value=0)
            if value is not None:
                cfg.min_image_height = value
                updated = True
        elif choice == "4":
            value = _prompt_float("Minimum aspect ratio", ratio_min, min_value=0.05, max_value=20.0)
            if value is not None:
                cfg.image_aspect_ratio_min = value
                if cfg.image_aspect_ratio_max < cfg.image_aspect_ratio_min:
                    cfg.image_aspect_ratio_max = cfg.image_aspect_ratio_min
                updated = True
        elif choice == "5":
            value = _prompt_float("Maximum aspect ratio", ratio_max, min_value=0.05, max_value=20.0)
            if value is not None:
                cfg.image_aspect_ratio_max = value
                if cfg.image_aspect_ratio_min > cfg.image_aspect_ratio_max:
                    cfg.image_aspect_ratio_min = cfg.image_aspect_ratio_max
                updated = True
        elif choice == "6":
            value = _prompt_bool("Enable blur filter", blur_enabled)
            if value is not None:
                cfg.image_blur_filter_enabled = value
                updated = True
        elif choice == "7":
            value = _prompt_float("Minimum blur score", blur_min, min_value=0.0, max_value=10000.0)
            if value is not None:
                cfg.image_blur_score_min = value
                updated = True
        elif choice == "8":
            value = _prompt_bool("Reject likely screenshots", screenshot_enabled)
            if value is not None:
                cfg.image_screenshot_filter_enabled = value
                updated = True
        elif choice == "9":
            value = _prompt_bool("Reject text-heavy images", text_enabled)
            if value is not None:
                cfg.image_text_heavy_filter_enabled = value
                updated = True
        elif choice == "10":
            value = _prompt_float(
                "Maximum text-heavy edge density",
                edge_limit,
                min_value=0.01,
                max_value=1.0,
            )
            if value is not None:
                cfg.image_text_heavy_max_edge_density = value
                updated = True
        else:
            print("Unknown image quality option.")

        if updated:
            if save_callback():
                print("Saved image quality settings.")
            else:
                print("Could not save image quality settings.")
        print()


def image_quality_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure image quality filters from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_image_quality() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, IMAGE_QUALITY_FIELD_NAMES, runtime_manager)
            return saved

        _image_quality_menu_impl(cfg.profile_name, cfg, save_single_image_quality)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Image Quality Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {image_quality_summary(bot_cfg)}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _image_quality_menu_impl(
            selected_cfg.profile_name,
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                IMAGE_QUALITY_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _domain_downloader_menu_impl(label: str, cfg: Config, save_callback) -> None:
    """Configure domain-specific media downloaders for one bot profile."""
    while True:
        enabled = bool(getattr(cfg, "domain_downloaders_enabled", True))
        imgur_enabled = bool(getattr(cfg, "imgur_album_downloads_enabled", True))
        html_enabled = bool(getattr(cfg, "html_media_resolver_enabled", True))

        print(f"Domain Downloaders for {label}:")
        print(f"  Domain downloaders: {'On' if enabled else 'Off'}")
        print(f"  Imgur albums: {'On' if imgur_enabled else 'Off'}")
        print(f"  Hosted-page resolver: {'On' if html_enabled else 'Off'}")
        print()
        print("1. Toggle domain downloaders")
        print("2. Toggle Imgur album downloads")
        print("3. Toggle hosted-page resolver")
        print("0. Back")
        print()

        choice = input("Select domain downloader option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            value = _prompt_bool("Enable domain-specific downloaders", enabled)
            if value is not None:
                cfg.domain_downloaders_enabled = value
                updated = True
        elif choice == "2":
            value = _prompt_bool("Enable Imgur album downloads", imgur_enabled)
            if value is not None:
                cfg.imgur_album_downloads_enabled = value
                updated = True
        elif choice == "3":
            value = _prompt_bool("Enable hosted-page media resolver", html_enabled)
            if value is not None:
                cfg.html_media_resolver_enabled = value
                updated = True
        else:
            print("Unknown domain downloader option.")

        if updated:
            if save_callback():
                print("Saved domain downloader settings.")
                print("Restart running bots to refresh active downloader handlers.")
            else:
                print("Could not save domain downloader settings.")
        print()


def domain_downloader_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure domain-specific downloaders from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_domain_downloaders() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, DOMAIN_DOWNLOADER_FIELD_NAMES, runtime_manager)
            return saved

        _domain_downloader_menu_impl(cfg.profile_name, cfg, save_single_domain_downloaders)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Domain Downloader Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            status = "on" if getattr(bot_cfg, "domain_downloaders_enabled", True) else "off"
            imgur_status = "imgur on" if getattr(bot_cfg, "imgur_album_downloads_enabled", True) else "imgur off"
            page_status = "page on" if getattr(bot_cfg, "html_media_resolver_enabled", True) else "page off"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {status}, {imgur_status}, {page_status}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _domain_downloader_menu_impl(
            selected_cfg.profile_name,
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                DOMAIN_DOWNLOADER_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _format_subreddit_rule_summary(subreddit: str, rule: Dict[str, Any]) -> str:
    """Format per-subreddit rule fields for CLI display."""
    parts = []
    if "min_upvotes" in rule:
        parts.append(f"min {rule['min_upvotes']} upvotes")
    if "max_post_age_hours" in rule:
        parts.append(f"max age {rule['max_post_age_hours']}h")
    if rule.get("media_type") and rule.get("media_type") != "any":
        parts.append(f"{rule['media_type']} only")
    if "skip_nsfw" in rule:
        parts.append("skip NSFW" if rule["skip_nsfw"] else "allow NSFW")
    if rule.get("caption_footer"):
        footer = str(rule["caption_footer"])
        if len(footer) > 42:
            footer = footer[:39] + "..."
        parts.append(f"footer '{footer}'")
    if rule.get("caption_template"):
        template = str(rule["caption_template"])
        if len(template) > 42:
            template = template[:39] + "..."
        parts.append(f"template '{template}'")
    if rule.get("caption_variants"):
        parts.append(f"{len(rule['caption_variants'])} caption variants")
    if "priority_weight" in rule:
        parts.append(f"weight {rule['priority_weight']:g}")

    summary = "; ".join(parts) if parts else "configured"
    return f"r/{subreddit}: {summary}"


def _choose_subreddit_for_rules(cfg: Config) -> Optional[str]:
    """Prompt for a configured subreddit to edit rules for."""
    if not cfg.subreddits:
        print("No subreddits configured.")
        return None

    print("Configured subreddits:")
    for idx, sub in enumerate(cfg.subreddits, 1):
        marker = " *" if cfg.get_subreddit_rule(sub) else ""
        print(f"  {idx}. r/{sub}{marker}")
    print()

    raw = input("Enter subreddit number or name: ").strip()
    if not raw:
        print("No subreddit selected.")
        return None

    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(cfg.subreddits):
            return cfg.normalize_subreddit_name(cfg.subreddits[index])
        print("Unknown subreddit number.")
        return None

    subreddit = cfg.normalize_subreddit_name(raw)
    configured = {cfg.normalize_subreddit_name(sub) for sub in cfg.subreddits}
    if subreddit not in configured:
        print("That subreddit is not in this bot profile. Add it first.")
        return None
    return subreddit


def _prompt_rule_int(label: str, current: Optional[int]) -> tuple[bool, Optional[int]]:
    """Prompt for an optional integer rule field."""
    current_text = str(current) if current is not None else "global"
    raw = input(f"{label} (current: {current_text}, blank keep, clear unset): ").strip()
    if not raw:
        return False, current
    if raw.lower() == "clear":
        return True, None
    try:
        value = int(raw)
    except ValueError:
        print("Please enter a whole number.")
        return False, current
    if value < 0:
        print("Value cannot be negative.")
        return False, current
    return True, value


def _prompt_rule_float(label: str, current: Optional[float]) -> tuple[bool, Optional[float]]:
    """Prompt for an optional float rule field."""
    current_text = f"{current:g}" if current is not None else "global"
    raw = input(f"{label} (current: {current_text}, blank keep, clear unset): ").strip()
    if not raw:
        return False, current
    if raw.lower() == "clear":
        return True, None
    try:
        value = float(raw)
    except ValueError:
        print("Please enter a number.")
        return False, current
    if value < 0:
        print("Value cannot be negative.")
        return False, current
    return True, value


def _prompt_rule_bool(label: str, current: Optional[bool]) -> tuple[bool, Optional[bool]]:
    """Prompt for an optional boolean rule field."""
    current_text = "global" if current is None else ("on" if current else "off")
    raw = input(f"{label} (current: {current_text}, y/n, blank keep, clear unset): ").strip().lower()
    if not raw:
        return False, current
    if raw == "clear":
        return True, None
    if raw in {"y", "yes", "1", "true", "on"}:
        return True, True
    if raw in {"n", "no", "0", "false", "off"}:
        return True, False
    print("Please answer y, n, or clear.")
    return False, current


def _prompt_rule_media_type(current: Optional[str]) -> tuple[bool, Optional[str]]:
    """Prompt for an optional media-type rule field."""
    current_text = current or "any/global"
    raw = (
        input(f"Media type (current: {current_text}, any/image/video/gallery, blank keep, clear unset): ")
        .strip()
        .lower()
    )
    if not raw:
        return False, current
    if raw == "clear":
        return True, None
    aliases = {
        "all": "any",
        "images": "image",
        "videos": "video",
        "albums": "gallery",
        "galleries": "gallery",
    }
    value = aliases.get(raw, raw)
    if value not in {"any", "image", "video", "gallery"}:
        print("Please enter any, image, video, gallery, or clear.")
        return False, current
    return True, value


def _prompt_rule_text(label: str, current: Optional[str]) -> tuple[bool, Optional[str]]:
    """Prompt for an optional text rule field."""
    current_text = current if current else "global/none"
    raw = input(f"{label} (current: {current_text}, blank keep, clear unset): ").strip()
    if not raw:
        return False, current
    if raw.lower() == "clear":
        return True, None
    return True, raw


def _prompt_rule_time(label: str, current: Optional[str]) -> tuple[bool, Optional[str]]:
    """Prompt for an optional HH:MM time value."""
    current_text = current if current else "global/none"
    raw = input(f"{label} (current: {current_text}, HH:MM, blank keep, clear unset): ").strip()
    if not raw:
        return False, current
    if raw.lower() == "clear":
        return True, None
    value = Config()._clean_time_text(raw)
    if not value:
        print("Please enter a valid time like 08:00.")
        return False, current
    return True, value


def _subreddit_rules_menu_impl(cfg: Config, save_callback) -> None:
    """Configure per-subreddit rules for one concrete bot profile."""
    while True:
        print("Subreddit Rules:")
        rules = getattr(cfg, "subreddit_rules", {}) or {}
        if rules:
            for subreddit in sorted(rules.keys()):
                print(f"  {_format_subreddit_rule_summary(subreddit, rules[subreddit])}")
        else:
            print("  No per-subreddit rules configured.")
        print()
        print("1. Add or edit rule")
        print("2. Remove rule")
        print("3. List subreddits")
        print("0. Back")
        print()

        choice = input("Select a rule option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice == "3":
            list_subreddits(cfg)
            print()
            continue

        if choice == "2":
            subreddit = _choose_subreddit_for_rules(cfg)
            if not subreddit:
                print()
                continue
            if subreddit not in cfg.subreddit_rules:
                print(f"No rule exists for r/{subreddit}.")
                print()
                continue
            cfg.subreddit_rules.pop(subreddit, None)
            if save_callback():
                print(f"Removed rule for r/{subreddit}.")
            else:
                print("Could not save subreddit rules.")
            print()
            continue

        if choice != "1":
            print("Unknown rule option.")
            print()
            continue

        subreddit = _choose_subreddit_for_rules(cfg)
        if not subreddit:
            print()
            continue

        rule = cfg.get_subreddit_rule(subreddit)
        print(f"Editing r/{subreddit}. Use blank to keep a value or 'clear' to unset it.")
        print()

        changed, value = _prompt_rule_int("Minimum upvotes", rule.get("min_upvotes"))
        if changed:
            if value is None:
                rule.pop("min_upvotes", None)
            else:
                rule["min_upvotes"] = value

        changed, value = _prompt_rule_int("Maximum post age in hours", rule.get("max_post_age_hours"))
        if changed:
            if value is None:
                rule.pop("max_post_age_hours", None)
            else:
                rule["max_post_age_hours"] = value

        changed, value = _prompt_rule_media_type(rule.get("media_type"))
        if changed:
            if value is None or value == "any":
                rule.pop("media_type", None)
            else:
                rule["media_type"] = value

        changed, value = _prompt_rule_bool("Skip NSFW for this subreddit", rule.get("skip_nsfw"))
        if changed:
            if value is None:
                rule.pop("skip_nsfw", None)
            else:
                rule["skip_nsfw"] = value

        _show_caption_placeholder_help()
        changed, value = _prompt_rule_text("Caption template", rule.get("caption_template"))
        if changed:
            if value is None:
                rule.pop("caption_template", None)
            else:
                rule["caption_template"] = value

        changed, value = _prompt_rule_text("Caption footer", rule.get("caption_footer"))
        if changed:
            if value is None:
                rule.pop("caption_footer", None)
            else:
                rule["caption_footer"] = value

        changed, value = _prompt_rule_float("Priority weight", rule.get("priority_weight"))
        if changed:
            if value is None:
                rule.pop("priority_weight", None)
            else:
                rule["priority_weight"] = value

        if rule:
            cleaned_rule = cfg._clean_subreddit_rules({subreddit: rule}).get(subreddit, {})
            if cleaned_rule:
                cfg.subreddit_rules[subreddit] = cleaned_rule
            else:
                cfg.subreddit_rules.pop(subreddit, None)
        else:
            cfg.subreddit_rules.pop(subreddit, None)

        if save_callback():
            print(f"Saved rule for r/{subreddit}.")
        else:
            print("Could not save subreddit rules.")
        print()


def subreddit_rules_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure per-subreddit rules from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_rules() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, SUBREDDIT_RULE_FIELD_NAMES, runtime_manager)
            return saved

        _subreddit_rules_menu_impl(cfg, save_single_rules)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Subreddit Rule Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            count = len(getattr(bot_cfg, "subreddit_rules", {}) or {})
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {count} rule(s)")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _subreddit_rules_menu_impl(
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                SUBREDDIT_RULE_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _format_scoring_weights(cfg: Config) -> str:
    """Return compact scoring weight text."""
    weights = getattr(cfg, "smart_scoring_weights", {}) or {}
    ordered = Config.SMART_SCORING_WEIGHT_DEFAULTS.keys()
    return ", ".join(f"{key}={float(weights.get(key, 0.0)):g}" for key in ordered)


def _scoring_options_impl(cfg: Config, save_callback) -> None:
    """Configure smart content scoring for one concrete bot profile."""
    while True:
        enabled = bool(getattr(cfg, "smart_scoring_enabled", True))
        top_pool = int(getattr(cfg, "smart_scoring_top_pool_size", 8) or 8)
        print("Smart Content Scoring:")
        print(f"  Enabled: {'On' if enabled else 'Off'}")
        print(f"  Top pool size: {top_pool}")
        print(f"  Weights: {_format_scoring_weights(cfg)}")
        print()
        print("1. Toggle scoring")
        print("2. Set top pool size")
        print("3. Edit component weights")
        print("4. Reset weights")
        print("0. Back")
        print()

        choice = input("Select a scoring option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False
        if choice == "1":
            value = _prompt_bool("Enable smart content scoring", enabled)
            if value is not None:
                cfg.smart_scoring_enabled = value
                updated = True
        elif choice == "2":
            value = _prompt_int("Top pool size", top_pool, min_value=1)
            if value is not None:
                cfg.smart_scoring_top_pool_size = value
                updated = True
        elif choice == "3":
            weights = dict(getattr(cfg, "smart_scoring_weights", {}) or {})
            for key, default_value in Config.SMART_SCORING_WEIGHT_DEFAULTS.items():
                current = float(weights.get(key, default_value))
                value = _prompt_float(
                    f"Weight for {key}",
                    current,
                    min_value=0.0,
                    max_value=10.0,
                )
                if value is not None:
                    weights[key] = value
                    updated = True
            if updated:
                cfg.smart_scoring_weights = cfg._clean_smart_scoring_weights(weights)
        elif choice == "4":
            cfg.smart_scoring_weights = dict(Config.SMART_SCORING_WEIGHT_DEFAULTS)
            updated = True
        else:
            print("Unknown scoring option.")

        if updated:
            if save_callback():
                print("Saved scoring settings.")
            else:
                print("Could not save scoring settings.")
            print()


def scoring_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure smart content scoring from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_scoring() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, SCORING_FIELD_NAMES, runtime_manager)
            return saved

        _scoring_options_impl(cfg, save_single_scoring)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Scoring Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            enabled = "On" if getattr(bot_cfg, "smart_scoring_enabled", True) else "Off"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | scoring {enabled}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _scoring_options_impl(
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                SCORING_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


def _format_time_range_list(ranges: List[Dict[str, Any]], *, peak: bool = False) -> str:
    """Format quiet or peak hours for CLI display."""
    if not ranges:
        return "none"
    parts = []
    for item in ranges:
        text = f"{item.get('start', '?')}-{item.get('end', '?')}"
        if peak and item.get("post_interval_minutes"):
            text += f" every {item['post_interval_minutes']}m"
        parts.append(text)
    return ", ".join(parts)


def _format_weekly_schedule_rule(key: str, rule: Dict[str, Any]) -> str:
    """Format one weekly schedule rule for CLI display."""
    parts = []
    if rule.get("paused"):
        parts.append("paused")
    if "post_interval_minutes" in rule:
        parts.append(f"{rule['post_interval_minutes']} min")
    if "post_interval_randomize" in rule:
        parts.append("randomized" if rule["post_interval_randomize"] else "fixed")
    if rule.get("active_hours_enabled"):
        parts.append(f"active {rule.get('active_hours_start', '?')}-{rule.get('active_hours_end', '?')}")
    if "daily_post_limit" in rule:
        limit = int(rule.get("daily_post_limit") or 0)
        parts.append("unlimited/day" if limit <= 0 else f"{limit}/day")
    if rule.get("quiet_hours"):
        parts.append(f"quiet {_format_time_range_list(rule['quiet_hours'])}")
    if rule.get("peak_hours"):
        parts.append(f"peak {_format_time_range_list(rule['peak_hours'], peak=True)}")
    return f"{key}: {'; '.join(parts) if parts else 'configured'}"


def _prompt_schedule_key(day_only: bool = False) -> Optional[str]:
    """Prompt for a weekly schedule key."""
    keys = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    if not day_only:
        keys = ["weekday", "weekend"] + keys

    print("Schedule keys:")
    for idx, key in enumerate(keys, 1):
        print(f"  {idx}. {key}")
    print()

    raw = input("Enter schedule key number or name: ").strip().lower()
    if not raw:
        print("No schedule key selected.")
        return None
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(keys):
            return keys[index]
        print("Unknown schedule key number.")
        return None

    key = Config.WEEKLY_SCHEDULE_ALIASES.get(raw, raw)
    if key in keys:
        return key
    print("Unknown schedule key.")
    return None


def _prompt_schedule_range(
    label: str,
    current: List[Dict[str, Any]],
    *,
    peak: bool = False,
) -> tuple[bool, List[Dict[str, Any]]]:
    """Prompt for a single quiet/peak range."""
    current_text = _format_time_range_list(current, peak=peak)
    example = "18:00-22:00,30" if peak else "00:00-08:00"
    raw = input(f"{label} (current: {current_text}, {example}, blank keep, clear unset): ").strip()
    if not raw:
        return False, current
    if raw.lower() == "clear":
        return True, []

    range_part = raw
    interval = None
    if peak and "," in raw:
        range_part, interval_raw = raw.split(",", 1)
        try:
            interval = int(interval_raw.strip())
        except ValueError:
            print("Peak interval must be a whole number of minutes.")
            return False, current
        if interval < 1:
            print("Peak interval must be at least 1 minute.")
            return False, current

    if "-" not in range_part:
        print("Use a range like 00:00-08:00.")
        return False, current
    start, end = [part.strip() for part in range_part.split("-", 1)]
    cfg_helper = Config()
    start = cfg_helper._clean_time_text(start)
    end = cfg_helper._clean_time_text(end)
    if not start or not end:
        print("Use valid HH:MM times.")
        return False, current

    item: Dict[str, Any] = {"start": start, "end": end}
    if peak:
        item["post_interval_minutes"] = interval or 30
    return True, [item]


def _weekly_schedule_menu_impl(cfg: Config, save_callback) -> None:
    """Configure weekly schedule for one concrete bot profile."""
    while True:
        print("Weekly Schedule:")
        print(f"  Enabled: {'On' if cfg.weekly_schedule_enabled else 'Off'}")
        print(f"  Timezone: {cfg.timezone}")
        if cfg.weekly_schedule:
            for key in [
                "weekday",
                "weekend",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ]:
                if key in cfg.weekly_schedule:
                    print(f"  {_format_weekly_schedule_rule(key, cfg.weekly_schedule[key])}")
        else:
            print("  No weekly overrides configured.")
        print()
        print("1. Toggle weekly schedule")
        print("2. Set timezone")
        print("3. Edit weekday profile")
        print("4. Edit weekend profile")
        print("5. Edit specific day")
        print("6. Remove schedule rule")
        print("0. Back")
        print()

        choice = input("Select a weekly schedule option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice == "1":
            value = _prompt_bool("Enable weekly schedule", cfg.weekly_schedule_enabled)
            if value is not None:
                cfg.weekly_schedule_enabled = value
                if save_callback():
                    print(f"Weekly schedule is now {'on' if value else 'off'}.")
                else:
                    print("Could not save weekly schedule.")
            print()
            continue

        if choice == "2":
            value = _prompt_text("Timezone", cfg.timezone)
            if value is not None:
                cfg.timezone = value
                if save_callback():
                    print("Timezone saved.")
                else:
                    print("Could not save timezone.")
            print()
            continue

        if choice == "3":
            key = "weekday"
        elif choice == "4":
            key = "weekend"
        elif choice == "5":
            key = _prompt_schedule_key(day_only=True)
            if not key:
                print()
                continue
        elif choice == "6":
            key = _prompt_schedule_key()
            if not key:
                print()
                continue
            if key not in cfg.weekly_schedule:
                print(f"No weekly schedule rule exists for {key}.")
            else:
                cfg.weekly_schedule.pop(key, None)
                if save_callback():
                    print(f"Removed weekly schedule rule for {key}.")
                else:
                    print("Could not save weekly schedule.")
            print()
            continue
        else:
            print("Unknown weekly schedule option.")
            print()
            continue

        rule = cfg.get_weekly_schedule_rule(key)
        print(f"Editing {key}. Use blank to keep a value or 'clear' to unset it.")
        print()

        changed, value = _prompt_rule_bool("Pause this schedule", rule.get("paused"))
        if changed:
            if value is None:
                rule.pop("paused", None)
            else:
                rule["paused"] = value

        changed, value = _prompt_rule_int("Post interval minutes", rule.get("post_interval_minutes"))
        if changed:
            if value is None:
                rule.pop("post_interval_minutes", None)
            else:
                rule["post_interval_minutes"] = value

        changed, value = _prompt_rule_bool("Randomize interval", rule.get("post_interval_randomize"))
        if changed:
            if value is None:
                rule.pop("post_interval_randomize", None)
            else:
                rule["post_interval_randomize"] = value

        changed, value = _prompt_rule_int(
            "Randomize range minutes",
            rule.get("randomize_range_minutes"),
        )
        if changed:
            if value is None:
                rule.pop("randomize_range_minutes", None)
            else:
                rule["randomize_range_minutes"] = value

        changed, value = _prompt_rule_bool("Enable active hours", rule.get("active_hours_enabled"))
        if changed:
            if value is None:
                rule.pop("active_hours_enabled", None)
            else:
                rule["active_hours_enabled"] = value

        changed, value = _prompt_rule_time("Active hours start", rule.get("active_hours_start"))
        if changed:
            if value is None:
                rule.pop("active_hours_start", None)
            else:
                rule["active_hours_start"] = value

        changed, value = _prompt_rule_time("Active hours end", rule.get("active_hours_end"))
        if changed:
            if value is None:
                rule.pop("active_hours_end", None)
            else:
                rule["active_hours_end"] = value

        changed, value = _prompt_rule_int("Daily post limit", rule.get("daily_post_limit"))
        if changed:
            if value is None:
                rule.pop("daily_post_limit", None)
            else:
                rule["daily_post_limit"] = value

        changed, ranges = _prompt_schedule_range("Quiet hours", rule.get("quiet_hours", []))
        if changed:
            if ranges:
                rule["quiet_hours"] = ranges
            else:
                rule.pop("quiet_hours", None)

        changed, ranges = _prompt_schedule_range(
            "Peak hours",
            rule.get("peak_hours", []),
            peak=True,
        )
        if changed:
            if ranges:
                rule["peak_hours"] = ranges
            else:
                rule.pop("peak_hours", None)

        if rule:
            cleaned_rule = cfg._clean_weekly_schedule({key: rule}).get(key, {})
            if cleaned_rule:
                cfg.weekly_schedule[key] = cleaned_rule
            else:
                cfg.weekly_schedule.pop(key, None)
        else:
            cfg.weekly_schedule.pop(key, None)

        if save_callback():
            print(f"Saved weekly schedule rule for {key}.")
        else:
            print("Could not save weekly schedule.")
        print()


def weekly_schedule_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure weekly schedule from the CLI."""
    if not cfg.is_multi_bot_config():

        def save_single_schedule() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, WEEKLY_SCHEDULE_FIELD_NAMES, runtime_manager)
            return saved

        _weekly_schedule_menu_impl(cfg, save_single_schedule)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Weekly Schedule Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            enabled = "On" if getattr(bot_cfg, "weekly_schedule_enabled", False) else "Off"
            count = len(getattr(bot_cfg, "weekly_schedule", {}) or {})
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {enabled}, {count} rule(s)")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        _weekly_schedule_menu_impl(
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                WEEKLY_SCHEDULE_FIELD_NAMES,
                runtime_manager,
            ),
        )
        entries = build_runtime_entries(cfg)


SETTINGS_FIELD_NAMES = [
    "post_interval_minutes",
    "post_interval_randomize",
    "randomize_range_minutes",
    "active_hours_enabled",
    "active_hours_start",
    "active_hours_end",
    "timezone",
    "weekly_schedule_enabled",
    "weekly_schedule",
    "daily_post_limit",
    "auto_approve_after_minutes",
    "approval_required",
    "max_previews_per_10min",
    "min_upvotes",
    "max_post_age_hours",
    "min_image_width",
    "min_image_height",
    "image_quality_rules_enabled",
    "image_aspect_ratio_min",
    "image_aspect_ratio_max",
    "image_blur_filter_enabled",
    "image_blur_score_min",
    "image_screenshot_filter_enabled",
    "image_text_heavy_filter_enabled",
    "image_text_heavy_max_edge_density",
    "skip_nsfw",
    "title_blacklist",
    "title_whitelist",
    "subreddit_rules",
    "max_images_in_row",
    "avoid_duplicate_subreddit_streak",
    "duplicate_crosspost_blocking",
    "duplicate_title_similarity_enabled",
    "duplicate_title_similarity_threshold",
    "duplicate_title_similarity_history_limit",
    "author_cooldown_enabled",
    "author_cooldown_hours",
    "smart_scoring_enabled",
    "smart_scoring_top_pool_size",
    "smart_scoring_weights",
    "caption_template",
    "caption_variants",
    "add_reddit_link_button",
    "add_subreddit_hashtag",
    "max_video_length_seconds",
    "video_audio_policy",
    "video_orientation_rule",
    "video_convert_to_mp4",
    "video_compression_enabled",
    "video_compression_target_mb",
    "max_download_mb",
    "gallery_posts_enabled",
    "min_gallery_items",
    "max_gallery_items",
    "domain_downloaders_enabled",
    "imgur_album_downloads_enabled",
    "html_media_resolver_enabled",
    "spoiler_posts_enabled",
]

CAPTION_FIELD_NAMES = [
    "caption_mode",
    "caption_template",
    "caption_footer_template",
    "caption_variants",
    "subreddit_rules",
]

REACTION_FIELD_NAMES = [
    "auto_reactions_enabled",
]

SUBREDDIT_RULE_FIELD_NAMES = [
    "subreddit_rules",
]

SCORING_FIELD_NAMES = [
    "smart_scoring_enabled",
    "smart_scoring_top_pool_size",
    "smart_scoring_weights",
]

WEEKLY_SCHEDULE_FIELD_NAMES = [
    "timezone",
    "weekly_schedule_enabled",
    "weekly_schedule",
]

DUPLICATE_DETECTION_FIELD_NAMES = [
    "duplicate_crosspost_blocking",
    "duplicate_title_similarity_enabled",
    "duplicate_title_similarity_threshold",
    "duplicate_title_similarity_history_limit",
    "author_cooldown_enabled",
    "author_cooldown_hours",
]

EMERGENCY_PAUSE_FIELD_NAMES = [
    "emergency_pause_enabled",
    "emergency_pause_window_minutes",
    "emergency_pause_thresholds",
    "emergency_pause_notify_admin",
]

AUTO_RECOVERY_FIELD_NAMES = [
    "auto_recovery_enabled",
    "auto_recovery_upload_retries",
    "auto_recovery_retry_delay_seconds",
    "auto_recovery_compress_on_retry",
    "auto_recovery_video_target_mb",
    "auto_recovery_image_target_mb",
    "auto_recovery_stuck_pending_minutes",
    "auto_recovery_notify_threshold",
    "auto_recovery_notify_window_minutes",
    "auto_recovery_notify_cooldown_minutes",
]

GALLERY_FIELD_NAMES = [
    "gallery_posts_enabled",
    "min_gallery_items",
    "max_gallery_items",
]

DOMAIN_DOWNLOADER_FIELD_NAMES = [
    "domain_downloaders_enabled",
    "imgur_album_downloads_enabled",
    "html_media_resolver_enabled",
]

VIDEO_RULE_FIELD_NAMES = [
    "max_video_length_seconds",
    "video_audio_policy",
    "video_orientation_rule",
    "video_convert_to_mp4",
    "video_compression_enabled",
    "video_compression_target_mb",
]

IMAGE_QUALITY_FIELD_NAMES = [
    "min_image_width",
    "min_image_height",
    "image_quality_rules_enabled",
    "image_aspect_ratio_min",
    "image_aspect_ratio_max",
    "image_blur_filter_enabled",
    "image_blur_score_min",
    "image_screenshot_filter_enabled",
    "image_text_heavy_filter_enabled",
    "image_text_heavy_max_edge_density",
]


def _clone_setting_value(value: Any) -> Any:
    """Clone simple config values before writing them back."""
    return copy.deepcopy(value)


def _persist_bot_fields(
    root_cfg: Config,
    bot_index: int,
    edited_cfg: Config,
    field_names: List[str],
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> bool:
    """Persist selected config fields back into a bot profile and runtime."""
    if bot_index < 0 or bot_index >= len(root_cfg.bots):
        return False

    for field in field_names:
        root_cfg.bots[bot_index][field] = _clone_setting_value(getattr(edited_cfg, field))

    saved = root_cfg.save()
    if not saved:
        return False

    entries = build_runtime_entries(root_cfg)
    if runtime_manager and 0 <= bot_index < len(entries):
        key = entries[bot_index]["key"]
        runtime = runtime_manager.runtimes.get(key)
        if runtime:
            runtime_cfg = runtime.get("config")
            if isinstance(runtime_cfg, Config):
                for field in field_names:
                    setattr(runtime_cfg, field, _clone_setting_value(getattr(edited_cfg, field)))

    return True


def _persist_bot_settings(
    root_cfg: Config,
    bot_index: int,
    edited_cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> bool:
    """Persist edited runtime settings back into the selected bot profile."""
    return _persist_bot_fields(
        root_cfg,
        bot_index,
        edited_cfg,
        SETTINGS_FIELD_NAMES,
        runtime_manager,
    )


def _sync_runtime_fields(
    cfg: Config,
    field_names: List[str],
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Sync updated fields into currently running bot configs."""
    if not runtime_manager:
        return

    for runtime in runtime_manager.runtimes.values():
        runtime_cfg = runtime.get("config")
        if isinstance(runtime_cfg, Config):
            for field in field_names:
                setattr(runtime_cfg, field, _clone_setting_value(getattr(cfg, field)))


def _show_caption_placeholder_help() -> None:
    """Print supported placeholders for caption text fields."""
    print("Placeholders you can use:")
    print("  {title} {subreddit} {author} {body}")
    print("  {permalink} {reddit_url} {url}")
    print()


def _format_caption_variant(index: int, variant: Dict[str, Any]) -> str:
    """Format one caption variant for CLI display."""
    mode = str(variant.get("mode") or "template")
    name = str(variant.get("name") or f"Variant {index}").strip()
    parts = [f"{index}. {name}", caption_mode_label(mode)]

    template = str(variant.get("template") or "").strip()
    footer = str(variant.get("footer_template") or "").strip()
    if template:
        parts.append(template[:64] + ("..." if len(template) > 64 else ""))
    if footer:
        parts.append("below: " + footer[:54] + ("..." if len(footer) > 54 else ""))

    return " | ".join(parts)


def _choose_caption_style_mode() -> Optional[str]:
    """Prompt for a built-in caption style variant mode."""
    options = {
        "1": "source",
        "2": "source_plus_body",
        "3": "source_with_credit",
        "4": "credit_only",
        "5": "none",
    }
    print("Variant style:")
    print("  1. Copy Reddit title")
    print("  2. Source title + body excerpt")
    print("  3. Source title + credit (r/subreddit)")
    print("  4. Credit only (r/subreddit)")
    print("  5. No caption")
    print("  0. Cancel")
    print()
    choice = input("Select a style: ").strip().lower()
    print()
    if choice in {"0", "cancel", ""}:
        return None
    return options.get(choice)


def _caption_variants_menu_impl(cfg: Config, save_callback) -> None:
    """Manage global caption variant rotation for one bot profile."""
    while True:
        variants = list(getattr(cfg, "caption_variants", []) or [])
        print("Caption Variants:")
        if variants:
            for index, variant in enumerate(variants, 1):
                print(f"  {_format_caption_variant(index, variant)}")
        else:
            print("  No caption variants configured.")
        print()
        print(f"Current mode: {caption_mode_label(getattr(cfg, 'caption_mode', 'template'))}")
        print("1. Enable variant rotation")
        print("2. Add custom template variant")
        print("3. Add Reddit title + text below variant")
        print("4. Add built-in style variant")
        print("5. Remove variant")
        print("6. Clear variants")
        print("0. Back")
        print()

        choice = input("Select a variant option: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        if choice == "1":
            if not variants:
                print("Add at least one variant before enabling rotation.")
            else:
                cfg.caption_mode = "variants"
                if save_callback():
                    print("Caption variant rotation enabled.")
                else:
                    print("Could not save caption variants.")
            print()
            continue

        if choice == "2":
            _show_caption_placeholder_help()
            template = input("Enter custom caption template: ").strip()
            if not template:
                print("Variant not added.")
                print()
                continue
            name = input("Variant name (blank uses default): ").strip()
            variant = {
                "name": name or f"Variant {len(variants) + 1}",
                "mode": "template",
                "template": template,
            }
            cfg.caption_variants = cfg._clean_caption_variants(variants + [variant])
            if save_callback():
                print("Custom caption variant added.")
            else:
                print("Could not save caption variants.")
            print()
            continue

        if choice == "3":
            _show_caption_placeholder_help()
            footer = input("Enter text below the Reddit title: ").strip()
            if not footer:
                print("Variant not added.")
                print()
                continue
            name = input("Variant name (blank uses default): ").strip()
            variant = {
                "name": name or f"Variant {len(variants) + 1}",
                "mode": "source_plus_footer",
                "footer_template": footer,
            }
            cfg.caption_variants = cfg._clean_caption_variants(variants + [variant])
            if save_callback():
                print("Title + text variant added.")
            else:
                print("Could not save caption variants.")
            print()
            continue

        if choice == "4":
            mode = _choose_caption_style_mode()
            if not mode:
                print("Variant not added.")
                print()
                continue
            name = input("Variant name (blank uses default): ").strip()
            variant = {
                "name": name or f"Variant {len(variants) + 1}",
                "mode": mode,
            }
            cfg.caption_variants = cfg._clean_caption_variants(variants + [variant])
            if save_callback():
                print("Built-in style variant added.")
            else:
                print("Could not save caption variants.")
            print()
            continue

        if choice == "5":
            raw = input("Variant number to remove: ").strip()
            if not raw.isdigit():
                print("Please enter a variant number.")
                print()
                continue
            index = int(raw) - 1
            if index < 0 or index >= len(variants):
                print("Variant not found.")
                print()
                continue
            removed = variants.pop(index)
            cfg.caption_variants = cfg._clean_caption_variants(variants)
            if cfg.caption_mode == "variants" and not cfg.caption_variants:
                cfg.caption_mode = "template"
            if save_callback():
                print(f"Removed variant: {removed.get('name', 'Variant')}")
            else:
                print("Could not save caption variants.")
            print()
            continue

        if choice == "6":
            confirm = input("Type CLEAR to remove all variants: ").strip()
            if confirm != "CLEAR":
                print("Clear cancelled.")
                print()
                continue
            cfg.caption_variants = []
            if cfg.caption_mode == "variants":
                cfg.caption_mode = "template"
            if save_callback():
                print("Caption variants cleared.")
            else:
                print("Could not save caption variants.")
            print()
            continue

        print("Unknown variant option.")
        print()


def _subreddit_caption_template_menu_impl(cfg: Config, save_callback) -> None:
    """Edit per-subreddit caption templates from the caption menu."""
    subreddit = _choose_subreddit_for_rules(cfg)
    if not subreddit:
        return

    rule = cfg.get_subreddit_rule(subreddit)
    current = str(rule.get("caption_template") or "").strip()
    print(f"Caption template for r/{subreddit}:")
    print(f"  Current: {current or '(none)'}")
    print("Use blank to keep the current value or type clear to remove it.")
    _show_caption_placeholder_help()

    value = input("New caption template: ").strip()
    if not value:
        print("Caption template not changed.")
        print()
        return
    if value.lower() == "clear":
        rule.pop("caption_template", None)
    else:
        rule["caption_template"] = value

    cleaned = cfg._clean_subreddit_rules({subreddit: rule}).get(subreddit, {})
    if cleaned:
        cfg.subreddit_rules[subreddit] = cleaned
    else:
        cfg.subreddit_rules.pop(subreddit, None)

    if save_callback():
        print(f"Saved caption template for r/{subreddit}.")
    else:
        print("Could not save per-subreddit caption template.")
    print()


def _caption_options_impl(cfg: Config, save_callback) -> None:
    """Configure caption behavior for a specific bot profile."""
    while True:
        current_mode = getattr(cfg, "caption_mode", "template")
        current_label = caption_mode_label(current_mode)
        custom_preview = (cfg.caption_template or "").strip()
        footer_preview = (getattr(cfg, "caption_footer_template", "") or "").strip()
        variant_count = len(getattr(cfg, "caption_variants", []) or [])
        template_rule_count = sum(
            1
            for rule in (getattr(cfg, "subreddit_rules", {}) or {}).values()
            if isinstance(rule, dict) and rule.get("caption_template")
        )

        print("Caption options:")
        print(f"  Current mode: {current_label}")
        print(f"  Caption variants: {variant_count}")
        print(f"  Per-subreddit templates: {template_rule_count}")
        if custom_preview:
            print(f"  Custom caption: {custom_preview[:120]}")
        if footer_preview:
            print(f"  Text below title: {footer_preview[:120]}")
        print()
        print("Choose a caption mode:")
        print("  1. Copy Reddit title")
        print("  2. Copy Reddit title + add text below")
        print("  3. Add custom caption")
        print("  4. Source title + body excerpt")
        print("  5. Source title + credit (r/subreddit)")
        print("  6. Credit only (r/subreddit)")
        print("  7. No caption")
        print("  T. Edit saved custom caption")
        print("  B. Edit saved text below Reddit title")
        print("  V. Manage caption variants")
        print("  S. Edit per-subreddit caption template")
        print("  0. Back")
        print()

        choice = input("Select caption option: ").strip().lower()
        print()

        mode_updates = {
            "1": "source",
            "4": "source_plus_body",
            "5": "source_with_credit",
            "6": "credit_only",
            "7": "none",
        }

        if choice in {"0", "back"}:
            return

        if choice in {"v", "variants", "variant"}:
            _caption_variants_menu_impl(cfg, save_callback)
            continue

        if choice in {"s", "subreddit", "sub"}:
            _subreddit_caption_template_menu_impl(cfg, save_callback)
            continue

        if choice in mode_updates:
            cfg.caption_mode = mode_updates[choice]
            if save_callback():
                print(f"Caption mode set to: {caption_mode_label(cfg.caption_mode)}")
                print("Changes apply to new posts immediately.")
            else:
                print("Could not save caption settings.")
            print()
            continue

        if choice in {"3", "t", "template", "edit"}:
            _show_caption_placeholder_help()
            prompt = "Enter custom caption"
            if custom_preview:
                prompt += " (leave blank to use the saved one)"
            new_template = input(f"{prompt}: ").strip()
            reused_saved_text = not new_template
            if not new_template:
                if not custom_preview:
                    print("Custom caption not changed.")
                    print()
                    continue
            else:
                cfg.caption_template = new_template
            cfg.caption_mode = "template"
            if save_callback():
                if reused_saved_text:
                    print("Saved custom caption enabled.")
                else:
                    print("Custom caption updated and enabled.")
            else:
                print("Could not save caption settings.")
            print()
            continue

        if choice in {"2", "below", "footer"}:
            choice = "b"

        if choice == "b":
            _show_caption_placeholder_help()
            prompt = "Enter the text to add below the Reddit title"
            if footer_preview:
                prompt += " (leave blank to use the saved one)"
            new_footer = input(f"{prompt}: ").strip()
            reused_saved_text = not new_footer
            if not new_footer:
                if not footer_preview:
                    print("Text below Reddit title not changed.")
                    print()
                    continue
            else:
                cfg.caption_footer_template = new_footer
            cfg.caption_mode = "source_plus_footer"
            if save_callback():
                if reused_saved_text:
                    print("Saved text below the Reddit title enabled.")
                else:
                    print("Caption text below the Reddit title updated and enabled.")
            else:
                print("Could not save caption settings.")
            print()
            continue

        print("Unknown caption option.")
        print()


def _settings_menu_impl(cfg: Config, save_callback) -> None:
    """Interactive settings editor for a concrete config profile."""

    while True:
        print("Settings:")
        print("1. Posting Interval (minutes)")
        print("2. Randomize Posting Interval")
        print("3. Randomize Range (minutes)")
        print("4. Active Hours Enabled")
        print("5. Active Hours Start (HH:MM)")
        print("6. Active Hours End (HH:MM)")
        print("7. Daily Post Limit (0 = unlimited)")
        print("8. Auto-Approve After (minutes)")
        print("9. Approval Required")
        print("10. Max Previews Per 10 Minutes")
        print("11. Minimum Upvotes")
        print("12. Maximum Post Age (hours)")
        print("13. Minimum Image Width (px)")
        print("14. Skip NSFW Content")
        print("15. Title Blacklist (comma-separated)")
        print("16. Title Whitelist (comma-separated)")
        print("17. Max Images In A Row")
        print("18. Avoid Duplicate Subreddit Streak")
        print("19. Caption Template")
        print("20. Add Reddit Link Button")
        print("21. Add Subreddit Hashtag")
        print("22. Max Video Length (seconds)")
        print("23. Max Download Size (MB)")
        print("24. Spoiler Effect On Posts")
        print("25. Gallery Posts Enabled")
        print("26. Minimum Gallery Items")
        print("27. Maximum Gallery Items")
        print("28. Domain Downloaders Enabled")
        print("29. Imgur Album Downloads Enabled")
        print("30. Hosted Page Resolver Enabled")
        print("31. Video Audio Policy")
        print("32. Video Orientation Rule")
        print("33. Convert Videos To MP4")
        print("34. Video Compression Enabled")
        print("35. Video Compression Target MB")
        print("36. Image Quality Rules Enabled")
        print("37. Minimum Image Height (px)")
        print("38. Image Aspect Ratio Minimum")
        print("39. Image Aspect Ratio Maximum")
        print("40. Image Blur Filter Enabled")
        print("41. Minimum Image Blur Score")
        print("42. Screenshot Filter Enabled")
        print("43. Text-Heavy Image Filter Enabled")
        print("44. Text-Heavy Edge Density Limit")
        print("45. Reddit User-Agent")
        print("46. Reddit OAuth Client ID")
        print("47. Reddit OAuth Client Secret")
        print("0. Back")
        print()

        choice = input("Select a setting: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return

        updated = False

        if choice == "1":
            value = _prompt_int("Posting interval (minutes)", cfg.post_interval_minutes, min_value=1)
            if value is not None:
                cfg.post_interval_minutes = value
                updated = True
        elif choice == "2":
            value = _prompt_bool("Randomize posting interval", cfg.post_interval_randomize)
            if value is not None:
                cfg.post_interval_randomize = value
                updated = True
        elif choice == "3":
            value = _prompt_int("Randomize range (minutes)", cfg.randomize_range_minutes, min_value=0)
            if value is not None:
                cfg.randomize_range_minutes = value
                updated = True
        elif choice == "4":
            value = _prompt_bool("Active hours enabled", cfg.active_hours_enabled)
            if value is not None:
                cfg.active_hours_enabled = value
                updated = True
        elif choice == "5":
            value = _prompt_text("Active hours start (HH:MM)", cfg.active_hours_start)
            if value is not None:
                cfg.active_hours_start = value
                updated = True
        elif choice == "6":
            value = _prompt_text("Active hours end (HH:MM)", cfg.active_hours_end)
            if value is not None:
                cfg.active_hours_end = value
                updated = True
        elif choice == "7":
            value = _prompt_int("Daily post limit (0 = unlimited)", cfg.daily_post_limit, min_value=0)
            if value is not None:
                cfg.daily_post_limit = value
                updated = True
        elif choice == "8":
            value = _prompt_int("Auto-approve after (minutes)", cfg.auto_approve_after_minutes, min_value=0)
            if value is not None:
                cfg.auto_approve_after_minutes = value
                updated = True
        elif choice == "9":
            value = _prompt_bool("Approval required", cfg.approval_required)
            if value is not None:
                cfg.approval_required = value
                updated = True
        elif choice == "10":
            value = _prompt_int("Max previews per 10 minutes", cfg.max_previews_per_10min, min_value=1)
            if value is not None:
                cfg.max_previews_per_10min = value
                updated = True
        elif choice == "11":
            value = _prompt_int("Minimum upvotes", cfg.min_upvotes, min_value=0)
            if value is not None:
                cfg.min_upvotes = value
                updated = True
        elif choice == "12":
            value = _prompt_int("Maximum post age (hours)", cfg.max_post_age_hours, min_value=0)
            if value is not None:
                cfg.max_post_age_hours = value
                updated = True
        elif choice == "13":
            value = _prompt_int("Minimum image width (px)", cfg.min_image_width, min_value=0)
            if value is not None:
                cfg.min_image_width = value
                updated = True
        elif choice == "14":
            value = _prompt_bool("Skip NSFW content", cfg.skip_nsfw)
            if value is not None:
                cfg.skip_nsfw = value
                updated = True
        elif choice == "15":
            value = _prompt_list("Title blacklist", list(cfg.title_blacklist))
            if value is not None:
                cfg.title_blacklist = value
                updated = True
        elif choice == "16":
            value = _prompt_list("Title whitelist", list(cfg.title_whitelist))
            if value is not None:
                cfg.title_whitelist = value
                updated = True
        elif choice == "17":
            value = _prompt_int("Max images in a row", cfg.max_images_in_row, min_value=0)
            if value is not None:
                cfg.max_images_in_row = value
                updated = True
        elif choice == "18":
            value = _prompt_int(
                "Avoid duplicate subreddit streak",
                cfg.avoid_duplicate_subreddit_streak,
                min_value=0,
            )
            if value is not None:
                cfg.avoid_duplicate_subreddit_streak = value
                updated = True
        elif choice == "19":
            value = _prompt_text("Caption template", cfg.caption_template)
            if value is not None:
                cfg.caption_template = value
                updated = True
        elif choice == "20":
            value = _prompt_bool("Add Reddit link button", cfg.add_reddit_link_button)
            if value is not None:
                cfg.add_reddit_link_button = value
                updated = True
        elif choice == "21":
            value = _prompt_bool("Add subreddit hashtag", cfg.add_subreddit_hashtag)
            if value is not None:
                cfg.add_subreddit_hashtag = value
                updated = True
        elif choice == "22":
            value = _prompt_int(
                "Max video length (seconds)",
                cfg.max_video_length_seconds,
                min_value=0,
            )
            if value is not None:
                cfg.max_video_length_seconds = value
                updated = True
        elif choice == "23":
            value = _prompt_int("Max download size (MB)", cfg.max_download_mb, min_value=1)
            if value is not None:
                cfg.max_download_mb = value
                cfg.video_compression_target_mb = min(
                    int(getattr(cfg, "video_compression_target_mb", 40) or 40),
                    cfg.max_download_mb,
                )
                updated = True
        elif choice == "24":
            value = _prompt_bool("Enable spoiler effect on posted photos/videos", cfg.spoiler_posts_enabled)
            if value is not None:
                cfg.spoiler_posts_enabled = value
                updated = True
        elif choice == "25":
            value = _prompt_bool("Enable Reddit gallery posts", cfg.gallery_posts_enabled)
            if value is not None:
                cfg.gallery_posts_enabled = value
                updated = True
        elif choice == "26":
            value = _prompt_int("Minimum gallery items", cfg.min_gallery_items, min_value=2)
            if value is not None:
                cfg.min_gallery_items = min(10, value)
                if cfg.max_gallery_items < cfg.min_gallery_items:
                    cfg.max_gallery_items = cfg.min_gallery_items
                updated = True
        elif choice == "27":
            value = _prompt_int("Maximum gallery items", cfg.max_gallery_items, min_value=2)
            if value is not None:
                cfg.max_gallery_items = min(10, value)
                if cfg.min_gallery_items > cfg.max_gallery_items:
                    cfg.min_gallery_items = cfg.max_gallery_items
                updated = True
        elif choice == "28":
            value = _prompt_bool("Enable domain-specific downloaders", cfg.domain_downloaders_enabled)
            if value is not None:
                cfg.domain_downloaders_enabled = value
                updated = True
        elif choice == "29":
            value = _prompt_bool("Enable Imgur album downloads", cfg.imgur_album_downloads_enabled)
            if value is not None:
                cfg.imgur_album_downloads_enabled = value
                updated = True
        elif choice == "30":
            value = _prompt_bool("Enable hosted-page media resolver", cfg.html_media_resolver_enabled)
            if value is not None:
                cfg.html_media_resolver_enabled = value
                updated = True
        elif choice == "31":
            value = _prompt_video_audio_policy(cfg.video_audio_policy)
            if value is not None:
                cfg.video_audio_policy = value
                updated = True
        elif choice == "32":
            value = _prompt_video_orientation_rule(cfg.video_orientation_rule)
            if value is not None:
                cfg.video_orientation_rule = value
                updated = True
        elif choice == "33":
            value = _prompt_bool("Convert videos to MP4", cfg.video_convert_to_mp4)
            if value is not None:
                cfg.video_convert_to_mp4 = value
                updated = True
        elif choice == "34":
            value = _prompt_bool("Compress videos over target size", cfg.video_compression_enabled)
            if value is not None:
                cfg.video_compression_enabled = value
                updated = True
        elif choice == "35":
            value = _prompt_int(
                "Video compression target MB",
                cfg.video_compression_target_mb,
                min_value=1,
            )
            if value is not None:
                cfg.video_compression_target_mb = min(cfg.max_download_mb, value)
                updated = True
        elif choice == "36":
            value = _prompt_bool("Enable image quality rules", cfg.image_quality_rules_enabled)
            if value is not None:
                cfg.image_quality_rules_enabled = value
                updated = True
        elif choice == "37":
            value = _prompt_int("Minimum image height px (0 = disabled)", cfg.min_image_height, min_value=0)
            if value is not None:
                cfg.min_image_height = value
                updated = True
        elif choice == "38":
            value = _prompt_float(
                "Minimum image aspect ratio",
                cfg.image_aspect_ratio_min,
                min_value=0.05,
                max_value=20.0,
            )
            if value is not None:
                cfg.image_aspect_ratio_min = value
                if cfg.image_aspect_ratio_max < cfg.image_aspect_ratio_min:
                    cfg.image_aspect_ratio_max = cfg.image_aspect_ratio_min
                updated = True
        elif choice == "39":
            value = _prompt_float(
                "Maximum image aspect ratio",
                cfg.image_aspect_ratio_max,
                min_value=0.05,
                max_value=20.0,
            )
            if value is not None:
                cfg.image_aspect_ratio_max = value
                if cfg.image_aspect_ratio_min > cfg.image_aspect_ratio_max:
                    cfg.image_aspect_ratio_min = cfg.image_aspect_ratio_max
                updated = True
        elif choice == "40":
            value = _prompt_bool("Enable image blur filter", cfg.image_blur_filter_enabled)
            if value is not None:
                cfg.image_blur_filter_enabled = value
                updated = True
        elif choice == "41":
            value = _prompt_float(
                "Minimum image blur score",
                cfg.image_blur_score_min,
                min_value=0.0,
                max_value=10000.0,
            )
            if value is not None:
                cfg.image_blur_score_min = value
                updated = True
        elif choice == "42":
            value = _prompt_bool("Reject likely screenshots", cfg.image_screenshot_filter_enabled)
            if value is not None:
                cfg.image_screenshot_filter_enabled = value
                updated = True
        elif choice == "43":
            value = _prompt_bool("Reject text-heavy images", cfg.image_text_heavy_filter_enabled)
            if value is not None:
                cfg.image_text_heavy_filter_enabled = value
                updated = True
        elif choice == "44":
            value = _prompt_float(
                "Maximum text-heavy edge density",
                cfg.image_text_heavy_max_edge_density,
                min_value=0.01,
                max_value=1.0,
            )
            if value is not None:
                cfg.image_text_heavy_max_edge_density = value
                updated = True
        elif choice == "45":
            raw = input(f"Reddit user-agent (current: {cfg.user_agent}): ").strip()
            if raw:
                cfg.user_agent = raw
                updated = True
            else:
                print("No change.")
        elif choice == "46":
            current = cfg.reddit_client_id or "(empty)"
            raw = input(f"Reddit OAuth client ID (current: {current}; enter value or 'clear'): ").strip()
            if raw.lower() == "clear":
                cfg.reddit_client_id = ""
                updated = True
            elif raw:
                cfg.reddit_client_id = raw
                updated = True
            else:
                print("No change.")
        elif choice == "47":
            current = "set" if cfg.reddit_client_secret else "(empty)"
            raw = input(f"Reddit OAuth client secret (current: {current}; enter value or 'clear'): ").strip()
            if raw.lower() == "clear":
                cfg.reddit_client_secret = ""
                updated = True
            elif raw:
                cfg.reddit_client_secret = raw
                updated = True
            else:
                print("No change.")
        else:
            print("Unknown setting.")

        if updated:
            if save_callback():
                print("Saved.")
            else:
                print("Could not save settings.")

        print()


def settings_menu(cfg: Config, runtime_manager: Optional[BotRuntimeManager] = None) -> None:
    """Interactive settings menu for single-bot or selected multi-bot profiles."""
    if not cfg.is_multi_bot_config():
        _settings_menu_impl(cfg, cfg.save)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Settings Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            nsfw_text = "allow" if not bot_cfg.skip_nsfw else "skip"
            print(
                f"{idx}. {bot_cfg.profile_name} -> {channel} | "
                f"NSFW: {nsfw_text} | Max age: {bot_cfg.max_post_age_hours}h"
            )
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        print(f"Editing settings for {selected_cfg.profile_name}")
        print("Changes to running bots may require a restart to fully apply.")
        print()

        _settings_menu_impl(
            selected_cfg,
            lambda: _persist_bot_settings(cfg, index, selected_cfg, runtime_manager),
        )

        entries = build_runtime_entries(cfg)


def delete_saved_data(
    runtime_manager: BotRuntimeManager,
    cfg: Config,
    config_path: str,
    state_path: str,
) -> bool:
    """Delete persisted state (and optionally config) with confirmation."""
    if runtime_manager.running_count() > 0:
        print("Stopping bot before deleting saved data...")
        still_running = runtime_manager.stop_all()
        if still_running:
            print("Some bots are still winding down:")
            for name in still_running:
                print(f"  - {name}")

    print("Delete saved data:")
    print("1. Delete state only (keeps login/config)")
    print("2. Delete state + config (forces setup next run)")
    print("0. Cancel")
    print()

    choice = input("Select an option: ").strip().lower()
    if choice in {"0", "c", "cancel", ""}:
        print("Delete cancelled.")
        return False

    if choice not in {"1", "2"}:
        print("Unknown option. Delete cancelled.")
        return False

    confirm = input("Type DELETE to confirm: ").strip()
    if confirm != "DELETE":
        print("Confirmation mismatch. Delete cancelled.")
        return False

    deleted_any = False
    config_label = os.path.basename(config_path)
    state_paths = _collect_known_state_paths(cfg)

    # Always delete state if requested.
    deleted_states = 0
    for path in state_paths:
        label = os.path.relpath(path, APP_BASE_DIR) if path.startswith(APP_BASE_DIR) else path
        if os.path.exists(path):
            try:
                os.remove(path)
                deleted_any = True
                deleted_states += 1
                print(f"Deleted {label}")
            except Exception as exc:
                print(f"Could not delete {label}: {exc}")

    if deleted_states == 0:
        print(f"{os.path.basename(state_path)} not found")

    if os.path.isdir(MULTI_STATE_DIR):
        try:
            if not os.listdir(MULTI_STATE_DIR):
                os.rmdir(MULTI_STATE_DIR)
        except Exception:
            pass

    config_deleted = False
    if choice == "2":
        if os.path.exists(config_path):
            try:
                os.remove(config_path)
                deleted_any = True
                config_deleted = True
                print(f"Deleted {config_label}")
            except Exception as exc:
                print(f"Could not delete {config_label}: {exc}")
        else:
            print(f"{config_label} not found")

    if not deleted_any:
        print("No files were deleted.")

    return config_deleted


def caption_options(
    cfg: Config,
    runtime_manager: Optional[BotRuntimeManager] = None,
) -> None:
    """Configure caption behavior for posts."""
    if not cfg.is_multi_bot_config():

        def save_single_caption_settings() -> bool:
            saved = cfg.save()
            if saved:
                _sync_runtime_fields(cfg, CAPTION_FIELD_NAMES, runtime_manager)
            return saved

        _caption_options_impl(cfg, save_single_caption_settings)
        return

    entries = build_runtime_entries(cfg)
    if not entries:
        print("No bot profiles configured.")
        return

    while True:
        print("Caption Profiles:")
        for idx, entry in enumerate(entries, 1):
            bot_cfg = entry["config"]
            channel = bot_cfg.get_default_channel() or "(no channel)"
            print(f"{idx}. {bot_cfg.profile_name} -> {channel} | {caption_mode_label(bot_cfg.caption_mode)}")
        print("0. Back")
        print()

        choice = input("Select a bot profile to edit: ").strip().lower()
        print()

        if choice in {"0", "back", "b", ""}:
            return
        if not choice.isdigit():
            print("Unknown bot profile.")
            print()
            continue

        index = int(choice) - 1
        if index < 0 or index >= len(entries):
            print("Unknown bot profile.")
            print()
            continue

        selected_cfg = entries[index]["config"]
        print(f"Editing caption options for {selected_cfg.profile_name}")
        print("Changes to running bots apply to future posts.")
        print()

        _caption_options_impl(
            selected_cfg,
            lambda: _persist_bot_fields(
                cfg,
                index,
                selected_cfg,
                CAPTION_FIELD_NAMES,
                runtime_manager,
            ),
        )

        entries = build_runtime_entries(cfg)


def show_statistics(cfg: Config) -> None:
    """Display basic bot statistics."""
    entries = load_runtime_entries(cfg)

    if cfg.is_multi_bot_config():
        total_posts = 0
        total_approvals = 0
        total_skips = 0
        total_today = 0

        print("Statistics by bot:")
        for entry in entries:
            bot_cfg = entry["config"]
            st = entry["state"]
            stats = st.get_stats()
            daily = st.get_daily_posts_count()
            posted_count = len(st.get_posted_posts())
            skipped_count = len(st.get_skipped_posts())
            pending_text = "yes" if st.has_pending() else "no"
            state_label = os.path.relpath(entry["state_path"], APP_BASE_DIR)

            total_posts += stats.get("total_posts", 0)
            total_approvals += stats.get("total_approvals", 0)
            total_skips += stats.get("total_skips", 0)
            total_today += daily

            print(f"  {bot_cfg.profile_name}:")
            print(f"    Channel: {bot_cfg.get_default_channel() or 'Not set'}")
            print(f"    Total posts: {stats.get('total_posts', 0)}")
            print(f"    Approved: {stats.get('total_approvals', 0)}")
            print(f"    Skipped: {stats.get('total_skips', 0)}")
            print(f"    Posted tracked: {posted_count}")
            print(f"    Skipped tracked: {skipped_count}")
            print(f"    Approval rate: {st.get_approval_rate():.1f}%")
            print(f"    Posted today: {daily}")
            print(f"    Pending: {pending_text}")
            print(f"    State file: {state_label}")

        print()
        print("Totals:")
        print(f"  Total posts: {total_posts}")
        print(f"  Total approvals: {total_approvals}")
        print(f"  Total skips: {total_skips}")
        print(f"  Posted today: {total_today}")
        return

    st = entries[0]["state"] if entries else load_state()
    stats = st.get_stats()
    daily = st.get_daily_posts_count()
    approval_rate = st.get_approval_rate()
    history = st.get_history(limit=5)
    posted_count = len(st.get_posted_posts())
    skipped_count = len(st.get_skipped_posts())

    print("Statistics:")
    print(f"  Total posts: {stats.get('total_posts', 0)}")
    print(f"  Total approvals: {stats.get('total_approvals', 0)}")
    print(f"  Total skips: {stats.get('total_skips', 0)}")
    print(f"  Posted tracked: {posted_count}")
    print(f"  Skipped tracked: {skipped_count}")
    print(f"  Approval rate: {approval_rate:.1f}%")
    print(f"  Posted today: {daily}")
    if history:
        print("  Recent posts:")
        for item in history:
            sub = item.get("subreddit", "?")
            pid = item.get("id", "?")
            ptype = item.get("type", "?")
            print(f"    - r/{sub} ({ptype}) [{pid}]")


def main_menu() -> int:
    """Run the interactive main menu."""
    runtime_manager = BotRuntimeManager()
    dashboard_manager = DashboardRuntimeManager(runtime_manager)

    try:
        while True:
            if not check_setup():
                print("No configuration found.")
                print(f"Expected config at: {CONFIG_PATH}")
                print("Please run setup first:")
                print("  py -3 setup_wizard.py")
                return 1

            cfg = load_config()
            entries = load_runtime_entries(cfg)

            render_menu(runtime_manager, dashboard_manager, cfg, entries)
            choice = input("Select an option: ").strip().lower()
            print()
            pause_after_action = True

            if choice in {"1", "start", "s"}:
                valid, error = cfg.validate()
                if not valid:
                    print(f"Configuration error: {error}")
                else:
                    set_runtime_paused(cfg, False)
                    started, already_running = runtime_manager.start(cfg)
                    if started:
                        print(f"Started {len(started)} bot(s): {', '.join(started)}")
                    elif already_running:
                        print("All configured bots are already running.")
                    else:
                        print("No bots were started.")
            elif choice in {"2", "stop", "p", "pause"}:
                set_runtime_paused(cfg, True)
                still_running = runtime_manager.stop_all()
                if still_running:
                    print("Bots are stopping in background:")
                    for name in still_running:
                        print(f"  - {name}")
                else:
                    print("All bots stopped.")
            elif choice in {"3", "add", "a"}:
                add_subreddit(cfg)
                pause_after_action = False
            elif choice in {"4", "remove", "r", "rm"}:
                remove_subreddit(cfg)
                pause_after_action = False
            elif choice in {"5", "list", "l"}:
                list_subreddits(cfg)
            elif choice in {"6", "interval", "i"}:
                change_interval(cfg)
            elif choice in {"7", "limit", "daily", "d"}:
                change_daily_limit(cfg)
            elif choice in {"8", "stats"}:
                show_statistics(cfg)
            elif choice in {"9", "caption", "cap", "c"}:
                caption_options(cfg, runtime_manager)
            elif choice in {"10", "delete", "wipe", "reset", "clear"}:
                config_deleted = delete_saved_data(runtime_manager, cfg, CONFIG_PATH, STATE_PATH)
                if config_deleted:
                    print()
                    print("Configuration deleted. Please run setup again:")
                    print("  py -3 setup_wizard.py")
                    return 1
            elif choice in {"11", "settings", "config", "cfg"}:
                settings_menu(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"12", "addbot", "bot", "account"}:
                add_bot_profile(cfg, runtime_manager)
            elif choice in {"13", "spoiler", "spoilers"}:
                spoiler_options(cfg, runtime_manager)
            elif choice in {"14", "reaction", "reactions", "emoji"}:
                reaction_options(cfg, runtime_manager)
            elif choice in {"15", "queue"}:
                queue_options(cfg, runtime_manager)
            elif choice in {"16", "rules", "subreddit-rules", "subrules"}:
                subreddit_rules_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"17", "scoring", "score", "smart"}:
                scoring_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"18", "weekly", "schedule", "weekly-schedule"}:
                weekly_schedule_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"19", "analytics", "performance"}:
                analytics_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"20", "dedupe", "duplicates", "duplicate", "duplicate-detection"}:
                duplicate_detection_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"22", "emergency", "emergency-pause", "safety"}:
                emergency_pause_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"23", "backup", "backups", "restore", "export"}:
                config_backup_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"24", "dashboard", "local-dashboard", "web"}:
                dashboard_options(dashboard_manager)
                pause_after_action = False
            elif choice in {"25", "gallery", "galleries", "albums"}:
                gallery_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"26", "domain", "domains", "downloaders", "domain-downloaders"}:
                domain_downloader_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"27", "video", "videos", "video-rules", "video_rules"}:
                video_rule_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"28", "image", "images", "image-quality", "quality"}:
                image_quality_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"29", "health", "health-check", "check"}:
                health_check_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"30", "errors", "error", "logs", "error-logs"}:
                error_log_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"31", "recovery", "auto-recovery", "autorecovery"}:
                auto_recovery_options(cfg, runtime_manager)
                pause_after_action = False
            elif choice in {"0", "close", "exit", "q", "quit"}:
                dashboard_manager.stop()
                runtime_manager.stop_all()
                return 0
            else:
                print("Unknown option.")

            if pause_after_action:
                print()
                input("Press Enter to return to the menu...")
                print()
    finally:
        dashboard_manager.stop()


def main() -> int:
    """Main entry point."""
    enable_utf8_console()
    enable_ansi_colors()

    try:
        # Check if setup is needed
        if not check_setup():
            print("No configuration found.\n")
            print("Launching the setup wizard...\n")
            if not run_setup_wizard():
                print("\nSetup did not complete. Exiting.")
                return 1
            print("\nSetup complete. Opening the main menu...\n")

        return main_menu()

    except KeyboardInterrupt:
        print("\n")
        log("Bot stopped by user")
        return 0

    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        print("\nFull traceback:")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
