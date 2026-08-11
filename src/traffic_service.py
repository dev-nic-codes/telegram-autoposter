"""
Traffic analytics for bot commands.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict


SOURCE_ROOT = Path(__file__).resolve().parents[1]
TRAFFIC_DB_PATH = SOURCE_ROOT / "states" / "bot_traffic.sqlite"


class TrafficService:
    """Stores bot command traffic."""

    def __init__(self, config) -> None:
        self.config = config
        self.db_path = TRAFFIC_DB_PATH

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        with self._get_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_key TEXT NOT NULL,
                    profile_name TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    command TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_bot_events_profile_created
                ON bot_events (profile_key, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_bot_events_profile_command_created
                ON bot_events (profile_key, command, created_at DESC);
                """
            )

    def track_command(self, message: Dict[str, Any], command: str, *, is_admin: bool) -> None:
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        from_user = message.get("from", {}) if isinstance(message, dict) else {}
        chat_id = chat.get("id")

        if not isinstance(chat_id, int):
            return

        with self._get_connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_events (
                    profile_key,
                    profile_name,
                    chat_id,
                    user_id,
                    username,
                    first_name,
                    command,
                    is_admin,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.config.profile_key,
                    self.config.profile_name,
                    chat_id,
                    from_user.get("id"),
                    str(from_user.get("username") or "").strip() or None,
                    str(from_user.get("first_name") or "").strip() or None,
                    command,
                    int(bool(is_admin)),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def build_report(self) -> str:
        bot_summary = self._get_bot_summary()

        lines = [f"{self.config.profile_name} Traffic", ""]
        lines.append("Bot")
        lines.append(f"Users: {bot_summary['unique_users_all']} all-time, {bot_summary['unique_users_today']} today")
        lines.append(f"Commands: {bot_summary['commands_all']} all-time, {bot_summary['commands_today']} today")
        lines.append(f"/start: {bot_summary['starts_all']} all-time, {bot_summary['starts_today']} today")
        lines.append(f"/help: {bot_summary['helps_all']} all-time, {bot_summary['helps_today']} today")

        tracking_since = bot_summary.get("tracking_since")
        if tracking_since:
            lines.append(f"Tracking since: {tracking_since}")

        return "\n".join(lines)

    def _get_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        return connection

    def _get_bot_summary(self) -> Dict[str, Any]:
        today_start = datetime.now(timezone.utc).date().isoformat()

        with self._get_connection() as connection:

            def scalar(query: str, params: tuple[Any, ...]) -> int:
                return int(connection.execute(query, params).fetchone()[0])

            base_params = (self.config.profile_key,)
            today_params = (self.config.profile_key, today_start)

            unique_users_all = scalar(
                """
                SELECT COUNT(DISTINCT chat_id)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0
                """,
                base_params,
            )
            unique_users_today = scalar(
                """
                SELECT COUNT(DISTINCT chat_id)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0 AND created_at >= ?
                """,
                today_params,
            )
            commands_all = scalar(
                """
                SELECT COUNT(*)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0
                """,
                base_params,
            )
            commands_today = scalar(
                """
                SELECT COUNT(*)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0 AND created_at >= ?
                """,
                today_params,
            )
            starts_all = scalar(
                """
                SELECT COUNT(*)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0 AND command = '/start'
                """,
                base_params,
            )
            starts_today = scalar(
                """
                SELECT COUNT(*)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0 AND command = '/start' AND created_at >= ?
                """,
                today_params,
            )
            helps_all = scalar(
                """
                SELECT COUNT(*)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0 AND command = '/help'
                """,
                base_params,
            )
            helps_today = scalar(
                """
                SELECT COUNT(*)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0 AND command = '/help' AND created_at >= ?
                """,
                today_params,
            )
            tracking_since_row = connection.execute(
                """
                SELECT MIN(created_at)
                FROM bot_events
                WHERE profile_key = ? AND is_admin = 0
                """,
                base_params,
            ).fetchone()

        tracking_since = None
        if tracking_since_row and tracking_since_row[0]:
            tracking_since = str(tracking_since_row[0])[:10]

        return {
            "unique_users_all": unique_users_all,
            "unique_users_today": unique_users_today,
            "commands_all": commands_all,
            "commands_today": commands_today,
            "starts_all": starts_all,
            "starts_today": starts_today,
            "helps_all": helps_all,
            "helps_today": helps_today,
            "tracking_since": tracking_since,
        }
