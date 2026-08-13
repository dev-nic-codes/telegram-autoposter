"""
Main bot logic.
Orchestrates all components and runs the main loop.
"""

import time
import random
import re
import copy
import html
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Callable
from utils import log, now_utc, format_seconds, parse_time
from config import Config
from state_manager import StateManager
from telegram_handler import TelegramHandler
from reddit_handler import RedditHandler, RedditConfigurationError
from media_handler import MediaHandler
from filters import ContentFilter
from scheduler import Scheduler
from commands import CommandHandler
from traffic_service import TrafficService
from url_utils import url_matches_domain


@dataclass
class AdminMenuPendingInput:
    page: str
    field: str
    parser: str
    prompt: str
    rule_key: Optional[str] = None


class RedditTelegramBot:
    """Main bot orchestrator"""

    SEARCH_FETCH_LIMITS = (30, 50, 100)
    TITLE_KEYWORD_STOPWORDS = {
        "about",
        "after",
        "again",
        "also",
        "because",
        "been",
        "before",
        "being",
        "could",
        "does",
        "down",
        "from",
        "have",
        "here",
        "into",
        "just",
        "like",
        "more",
        "only",
        "over",
        "some",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "with",
        "would",
        "your",
    }
    ADMIN_MENU_COMMANDS = {
        "status": "/status",
        "stats": "/stats",
        "analytics": "/analytics",
        "digest": "/digest",
        "health": "/health",
        "health_quick": "/health quick",
        "errors": "/errors",
        "logs": "/logs",
        "errors_clear": "/errors clear",
        "recovery": "/recovery",
        "recovery_clear": "/recovery clear",
        "emergency": "/emergency",
        "emergency_reset": "/emergency reset",
        "traffic": "/traffic",
        "queue": "/queue",
        "queue_post": "/queue post",
        "queue_clear": "/queue clear",
        "blocks": "/blocks",
        "blocks_clear": "/blocks clear all",
        "rules": "/rules",
        "caption": "/caption",
        "dedupe": "/dedupe",
        "scoring": "/scoring",
        "schedule": "/schedule",
        "config": "/config",
        "subs": "/subs",
        "pause": "/pause",
        "resume": "/resume",
        "next": "/next",
    }
    ADMIN_MENU_TOGGLES = {
        "spoiler_posts_enabled": "🙈 Spoiler posts",
        "auto_reactions_enabled": "👍 Auto reactions",
        "add_reddit_link_button": "🔗 Reddit source button",
        "add_subreddit_hashtag": "#️⃣ Subreddit hashtags",
        "emergency_pause_enabled": "🚨 Emergency pause",
        "auto_recovery_enabled": "🛠️ Auto-recovery",
        "gallery_posts_enabled": "🖼️ Gallery posts",
        "domain_downloaders_enabled": "🌐 Domain downloaders",
        "imgur_album_downloads_enabled": "🧩 Imgur albums",
        "html_media_resolver_enabled": "📄 Hosted page resolver",
        "video_compression_enabled": "🎞️ Video compression",
        "video_convert_to_mp4": "🎬 MP4 conversion",
        "image_quality_rules_enabled": "🧼 Image quality rules",
        "smart_scoring_enabled": "🎯 Smart scoring",
        "duplicate_crosspost_blocking": "🧬 Crosspost blocking",
        "duplicate_title_similarity_enabled": "📝 Title dedupe",
        "author_cooldown_enabled": "👤 Author cooldown",
        "skip_nsfw": "🔞 Skip NSFW",
    }

    def __init__(
        self,
        config: Optional[Config] = None,
        config_path: str = "config.json",
        state_path: str = "state.json",
    ):
        """Initialize bot with configuration"""
        self.config = config or Config(config_path)
        self._config_preloaded = config is not None
        self.state = StateManager(state_path)
        self.running = False
        self._telegram_update_thread: Optional[threading.Thread] = None
        self._telegram_update_stop = threading.Event()
        self._telegram_update_lock = threading.Lock()
        self._posting_work_lock = threading.Lock()
        self._update_offset_dirty = False
        self._status_last_log_ts: float = 0.0
        self._status_interval_seconds: float = 8.0
        self._status_errors_today: int = 0
        self._status: Dict[str, str] = {
            "step": "Idle",
            "source": "-",
            "progress": "-",
            "result": "-",
            "event": "Starting",
        }
        self._last_channel_post_message: Optional[Dict[str, Any]] = None
        self._admin_menu_pending_inputs: Dict[int, AdminMenuPendingInput] = {}

        # Initialize handlers (will be set up in start())
        self.telegram: Optional[TelegramHandler] = None
        self.reddit: Optional[RedditHandler] = None
        self.media: Optional[MediaHandler] = None
        self.filter: Optional[ContentFilter] = None
        self.scheduler: Optional[Scheduler] = None
        self.commands: Optional[CommandHandler] = None
        self.traffic: Optional[TrafficService] = None

    def _status_last_post_text(self) -> str:
        """Return a short summary of the last posted item."""
        try:
            history = self.state.get_history(limit=1)
            if not history:
                return "none"
            item = history[0]
            ts = item.get("timestamp")
            subreddit = item.get("subreddit", "?")
            if not ts:
                return f"r/{subreddit}"
            dt_local = datetime.fromisoformat(ts).astimezone()
            return f"{dt_local:%H:%M} r/{subreddit}"
        except Exception:
            return "unknown"

    def _status_next_text(self) -> str:
        """Return time until the next scheduled post tick."""
        try:
            last_tick = self.state.get_last_tick()
            next_time = self.scheduler.calculate_next_post_time(last_tick)
            remaining_s = max(0, int((next_time - now_utc()).total_seconds()))
            return format_seconds(remaining_s)
        except Exception:
            return "?"

    def _status_pending_text(self) -> str:
        """Return pending item id (if any)."""
        pending = self.state.get_pending()
        if isinstance(pending, dict):
            return pending.get("id", "pending")
        return "none"

    def _local_now(self) -> datetime:
        """Return the bot schedule-local datetime."""
        try:
            if self.scheduler:
                return self.scheduler._local_now()
        except Exception:
            pass
        return now_utc().astimezone()

    def _local_date_key(self) -> str:
        """Return the local date key used for daily counters."""
        return self._local_now().date().isoformat()

    def _html_escape(self, value: Any) -> str:
        """Escape text for Telegram HTML parse mode."""
        return html.escape(str(value or ""), quote=False)

    def _format_admin_message_html(self, text: str, *, fallback_title: str = "🤖 Telegram Autoposter") -> str:
        """Escape a bot message and bold the first visible line."""
        raw = str(text or "").strip()
        if not raw:
            raw = fallback_title
        if len(raw) > 3800:
            raw = raw[:3797] + "..."

        lines = raw.splitlines()
        first_visible = None
        for index, line in enumerate(lines):
            if line.strip():
                first_visible = index
                break
        if first_visible is None:
            return f"<b>{self._html_escape(fallback_title)}</b>"

        if not any(ord(ch) >= 0x2500 for ch in lines[first_visible].strip()[:4]):
            lines[first_visible] = f"🤖 {lines[first_visible].strip()}"

        escaped = [self._html_escape(line) for line in lines]
        escaped[first_visible] = f"<b>{escaped[first_visible]}</b>"
        return "\n".join(escaped)

    def _format_menu_message_html(self, text: str, *, fallback_title: str = "🤖 Telegram Autoposter Control") -> str:
        """Return plain menu text with any legacy HTML tags stripped out."""
        raw = str(text or "").strip()
        if not raw:
            raw = fallback_title
        if len(raw) > 3800:
            raw = raw[:3797] + "..."
        cleaned = re.sub(r"</?(?:b|i|u|code|pre)>", "", raw, flags=re.IGNORECASE)
        return html.unescape(cleaned)

    def _send_admin_message_html(
        self,
        text: str,
        *,
        reply_markup: Optional[Dict[str, Any]] = None,
        disable_notification: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Send a formatted admin text message."""
        if not self.telegram:
            return None
        first_result: Optional[Dict[str, Any]] = None
        for admin_id in self.config.get_admin_user_ids():
            try:
                result = self.telegram.send_message(
                    admin_id,
                    self._format_admin_message_html(text),
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    parse_mode="HTML",
                )
                if first_result is None:
                    first_result = result
            except Exception as e:
                log(f"Could not send admin message to {admin_id}: {e}", "WARN")
        return first_result

    def _format_admin_actor(self, user: Optional[Dict[str, Any]]) -> str:
        """Render a readable admin label for audit notices."""
        payload = user or {}
        username = str(payload.get("username") or "").strip()
        if username:
            return f"@{username.lstrip('@')}"

        first = str(payload.get("first_name") or "").strip()
        last = str(payload.get("last_name") or "").strip()
        full = " ".join(part for part in (first, last) if part).strip()
        if full:
            return full

        user_id = payload.get("id")
        return f"admin {user_id}" if user_id else "an admin"

    def _notify_admin_pending_action(
        self,
        action: str,
        actor_label: str,
        pending: Optional[Dict[str, Any]],
    ) -> None:
        """Broadcast a short audit notice for admin approval actions."""
        if not pending:
            return

        title = str(pending.get("title") or "").strip()
        if len(title) > 90:
            title = title[:87] + "..."
        subreddit = str(pending.get("subreddit") or "?").strip()

        messages = {
            "approve": f"✅ Approved by {actor_label}\n\nr/{subreddit} - {title}",
        }
        text = messages.get(action)
        if text:
            self._send_admin_notice(text)

    def _menu_button(self, text: str, data: str) -> Dict[str, str]:
        """Build one inline menu button."""
        return {"text": text, "callback_data": data}

    def _menu_nav_keyboard(self, parent: str = "main", refresh_data: Optional[str] = None) -> Dict[str, Any]:
        """Return a standard menu navigation keyboard."""
        rows: List[List[Dict[str, str]]] = []
        if refresh_data:
            rows.append([self._menu_button("🔄 Refresh", refresh_data)])
        rows.append(
            [
                self._menu_button("⬅️ Back", f"menu:page:{parent}"),
                self._menu_button("🏠 Main", "menu:page:main"),
            ]
        )
        return self.telegram.create_inline_keyboard(rows)

    def _admin_menu_home_text(self, notice: str = "") -> str:
        """Return the admin menu home text."""
        pending = self.state.get_pending() if self.state.has_pending() else None
        daily = self.state.get_daily_posts_count(self._local_date_key())
        limit = self.scheduler.get_effective_daily_limit() if self.scheduler else 0
        channel = self.config.get_default_channel() or "not set"

        if self.state.get_emergency_pause():
            status = "🚨 Emergency paused"
        elif self.state.is_paused():
            status = "⏸️ Paused"
        else:
            status = "✅ Running"

        if pending:
            pending_text = f"r/{pending.get('subreddit', '?')} / {pending.get('id', 'pending')}"
        else:
            pending_text = "none"

        today_text = f"{daily}/{limit}" if limit > 0 else str(daily)
        lines = [
            "🤖 Telegram Autoposter",
            "",
            "Simple approval mode is active.",
            "",
            f"Status: {status}",
            f"Channel: {channel}",
            f"Pending: {pending_text}",
            f"Today: {today_text} post(s)",
            f"Next check: {self._status_next_text()}",
            "",
            "Use New Post to fetch a fresh preview, Post Now to publish it, or Skip to reject it.",
        ]
        if notice:
            lines.insert(2, f"Last action: {notice}")
            lines.insert(3, "")
        return "\n".join(lines)

    def _toggle_label(self, field: str) -> str:
        """Return a toggle label with current state."""
        label = self.ADMIN_MENU_TOGGLES.get(field, field)
        state = "On" if bool(getattr(self.config, field, False)) else "Off"
        return f"{label}: {state}"

    def send_admin_menu(self, chat_id: Any) -> None:
        """Send the editable admin control menu."""
        text, keyboard = self._admin_menu_page("main")
        self.telegram.send_message(
            chat_id,
            self._format_menu_message_html(text),
            reply_markup=keyboard,
            disable_notification=True,
        )

    def _posting_busy_text(self) -> str:
        """Return a short admin-facing busy message for actions that need the posting worker."""
        step = self._status.get("step") or "Working"
        source = self._status.get("source") or "-"
        progress = self._status.get("progress") or "-"
        event = self._status.get("event") or "busy"
        return (
            "⏳ Bot Busy\n\n"
            "A post search or upload is already running.\n"
            f"Current: {step} | {source} | {progress}\n"
            f"Details: {event}\n\n"
            "Menu navigation and status buttons still work. Try this action again when the current job finishes."
        )

    def _run_posting_work(
        self,
        label: str,
        callback: Callable[[], bool],
        *,
        notify_busy: bool = False,
    ) -> Optional[bool]:
        """Run a long posting/search action without allowing overlapping jobs."""
        if not self._posting_work_lock.acquire(blocking=False):
            log(f"Skipping {label}; posting worker is busy", "DEBUG")
            if notify_busy:
                self._send_admin_notice(self._posting_busy_text())
            return None

        try:
            return bool(callback())
        finally:
            self._posting_work_lock.release()

    def _schedule_pending_refresh(
        self,
        *,
        label: str,
        delay_seconds: float = 0.0,
        no_result_notice: Optional[str] = None,
        use_queue: bool = False,
        exclude_ids: Optional[set[str]] = None,
        exclude_urls: Optional[set[str]] = None,
        exclude_signatures: Optional[set[str]] = None,
    ) -> None:
        """Create the next preview in the background so admin callbacks stay responsive."""

        delay_seconds = max(0.0, float(delay_seconds or 0.0))
        exclude_ids_copy = set(exclude_ids or set())
        exclude_urls_copy = set(exclude_urls or set())
        exclude_signatures_copy = set(exclude_signatures or set())

        def worker() -> None:
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            if not self.running or self.state.is_paused() or self.state.has_pending():
                return

            result = self._run_posting_work(
                label,
                lambda: self.create_pending_approval(
                    use_queue=use_queue,
                    exclude_ids=exclude_ids_copy,
                    exclude_urls=exclude_urls_copy,
                    exclude_signatures=exclude_signatures_copy,
                ),
            )
            if result is False and no_result_notice:
                self._send_admin_notice(no_result_notice)

        threading.Thread(
            target=worker,
            name=f"autoposter-refresh-{int(time.time() * 1000)}",
            daemon=True,
        ).start()

    def _persist_deferred_update_offset(self) -> None:
        """Persist Telegram update offset once the posting worker is idle."""
        if not self._update_offset_dirty:
            return
        if self._posting_work_lock.locked():
            return
        if self.state.save():
            self._update_offset_dirty = False

    def _start_telegram_update_worker(self) -> None:
        """Start background Telegram polling so menus remain responsive during searches."""
        if self._telegram_update_thread and self._telegram_update_thread.is_alive():
            return

        self._telegram_update_stop.clear()
        profile_key = getattr(self.config, "profile_key", "default")
        self._telegram_update_thread = threading.Thread(
            target=self._telegram_update_worker,
            name=f"telegram-updates-{profile_key}",
            daemon=True,
        )
        self._telegram_update_thread.start()

    def _telegram_update_worker(self) -> None:
        """Poll Telegram in the background while the main loop handles posting."""
        log("Telegram menu worker started", "DEBUG")
        while self.running and not self._telegram_update_stop.is_set():
            try:
                self.process_telegram_updates(timeout=1)
                self._persist_deferred_update_offset()
            except Exception as e:
                log(f"Telegram menu worker error: {e}", "WARN")
            backoff = float(getattr(self.telegram, "get_updates_backoff_seconds", 0.0) or 0.0)
            self._telegram_update_stop.wait(max(0.05, backoff))

        self._persist_deferred_update_offset()
        log("Telegram menu worker stopped", "DEBUG")

    def _persist_runtime_config_fields(self, field_names: List[str]) -> bool:
        """Persist runtime config changes safely for single or multi-bot config files."""
        try:
            root = Config(self.config.config_path)
            if not root.load():
                return False

            if root.is_multi_bot_config():
                runtime_configs = root.build_runtime_configs()
                target_index = None
                for index, bot_cfg in enumerate(runtime_configs):
                    if bot_cfg.profile_key == self.config.profile_key:
                        target_index = index
                        break
                if target_index is None or target_index >= len(root.bots):
                    return False
                for field in field_names:
                    root.bots[target_index][field] = copy.deepcopy(getattr(self.config, field))
            else:
                for field in field_names:
                    setattr(root, field, copy.deepcopy(getattr(self.config, field)))

            return root.save()
        except Exception as e:
            log(f"Could not persist Telegram menu setting: {e}", "WARN")
            return False

    def _run_menu_command(self, command_key: str) -> str:
        """Run a registered admin command by menu command key."""
        command = self.ADMIN_MENU_COMMANDS.get(command_key)
        if not command or not self.commands:
            return "⚠️ Unknown menu command."
        response = self.commands.process_command(command, is_admin=True)
        return response or "✅ Done."

    def _execute_pending_action(self, action: str) -> bool:
        """Execute a pending-preview action. Caller must hold the posting work lock."""
        if action == "approve":
            return self.approve_pending()
        if action == "queue":
            return self.queue_pending()
        if action == "reroll":
            return self.reroll_pending()
        if action == "skip":
            return self.skip_pending()
        if action == "block_author":
            return self.block_pending_author()
        if action == "block_subreddit":
            return self.block_pending_subreddit()
        if action == "block_title_keyword":
            return self.block_pending_title_keyword()
        return False

    def _run_menu_action(self, action: str, actor: Optional[Dict[str, Any]] = None) -> str:
        """Run a direct menu action against pending previews."""
        messages = {
            "approve": ("✅ Pending preview posted.", "⚠️ No pending preview was posted."),
            "skip": ("⏭️ Pending preview skipped.", "⚠️ No pending preview was skipped."),
            "new_post": ("🐾 Looking for a new preview now.", "⚠️ Could not start a new preview search."),
        }
        if action not in messages:
            return "⚠️ This menu was simplified. Use Post Now, Skip, or New Post."

        if action == "new_post":
            if self.state.has_pending():
                return "📝 A preview is already pending. Use Post Now or Skip first."
            if self.state.is_paused() or self.state.get_emergency_pause():
                return "⏸️ Bot is paused. New preview search was not started."
            self.state.set_last_tick(None)
            self.state.save()
            self._schedule_pending_refresh(
                label="manual new preview",
                delay_seconds=0.0,
                no_result_notice="No new preview found right now.",
            )
            return messages[action][0]

        pending_snapshot = copy.deepcopy(self.state.get_pending()) if action in {"approve", "skip"} else None
        result = self._run_posting_work(
            f"Telegram menu action {action}",
            lambda: self._execute_pending_action(action),
        )
        if result is None:
            return self._posting_busy_text()

        if result and pending_snapshot and actor:
            self._notify_admin_pending_action(action, self._format_admin_actor(actor), pending_snapshot)

        success_message, failure_message = messages[action]
        return success_message if result else failure_message

    def _menu_toggle_labels(self) -> Dict[str, str]:
        return {
            "post_interval_randomize": "Randomize",
            "active_hours_enabled": "Active Hours",
            "approval_required": "Approval",
            "weekly_schedule_enabled": "Weekly",
            "spoiler_posts_enabled": "Spoiler",
            "auto_reactions_enabled": "Reactions",
            "add_reddit_link_button": "Source Btn",
            "add_subreddit_hashtag": "Hashtag",
            "emergency_pause_enabled": "Emergency",
            "auto_recovery_enabled": "Recovery",
            "gallery_posts_enabled": "Gallery",
            "domain_downloaders_enabled": "Domains",
            "imgur_album_downloads_enabled": "Imgur",
            "html_media_resolver_enabled": "Resolver",
            "video_compression_enabled": "Compress",
            "video_convert_to_mp4": "MP4",
            "image_quality_rules_enabled": "Image QC",
            "smart_scoring_enabled": "Scoring",
            "duplicate_crosspost_blocking": "Crosspost",
            "duplicate_title_similarity_enabled": "Title Match",
            "author_cooldown_enabled": "Author CD",
            "skip_nsfw": "NSFW Skip",
        }

    def _toggle_button_label(self, field: str) -> str:
        labels = self._menu_toggle_labels()
        label = labels.get(field, field)
        state = "🟢" if bool(getattr(self.config, field, False)) else "⚪"
        return f"{state} {label}"

    def _toggle_button(self, field: str, page: str) -> Dict[str, str]:
        return self._menu_button(self._toggle_button_label(field), f"menu:tog:{page}:{field}")

    def _caption_mode_label(self, mode: str) -> str:
        """Return a short readable caption mode label."""
        labels = {
            "template": "Custom caption",
            "source": "Reddit post title",
            "none": "No caption",
        }
        return labels.get(str(mode or "template").strip().lower(), "Custom caption")

    def _format_weekly_ranges(self, ranges: List[Dict[str, Any]]) -> str:
        if not ranges:
            return "none"
        parts: List[str] = []
        for item in ranges:
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            if start and end:
                parts.append(f"{start}-{end}")
        return ", ".join(parts) if parts else "none"

    def _format_weekly_peak_ranges(self, ranges: List[Dict[str, Any]]) -> str:
        if not ranges:
            return "none"
        parts: List[str] = []
        for item in ranges:
            start = str(item.get("start") or "").strip()
            end = str(item.get("end") or "").strip()
            interval = item.get("post_interval_minutes")
            if start and end and interval:
                parts.append(f"{start}-{end}={int(interval)}")
        return ", ".join(parts) if parts else "none"

    def _send_admin_menu_page(self, chat_id: Any, page: str, notice: str = "") -> None:
        text, keyboard = self._admin_menu_page(page, notice=notice)
        self.telegram.send_message(
            chat_id,
            self._format_menu_message_html(text),
            reply_markup=keyboard,
            disable_notification=True,
        )

    def _queue_admin_menu_input(
        self,
        user_id: int,
        *,
        page: str,
        field: str,
        parser: str,
        prompt: str,
        rule_key: Optional[str] = None,
    ) -> None:
        self._admin_menu_pending_inputs[user_id] = AdminMenuPendingInput(
            page=page,
            field=field,
            parser=parser,
            prompt=prompt,
            rule_key=rule_key,
        )

    def _parse_menu_time_value(self, raw: str) -> str:
        parsed = parse_time(raw)
        if not parsed:
            raise ValueError("Use HH:MM.")
        hour, minute = parsed
        return f"{hour:02d}:{minute:02d}"

    def _parse_menu_ranges(self, raw: str) -> List[Dict[str, str]]:
        text = raw.strip()
        if not text or text.casefold() in {"none", "clear", "off"}:
            return []
        ranges: List[Dict[str, str]] = []
        for chunk in text.split(","):
            item = chunk.strip()
            if "-" not in item:
                raise ValueError("Use HH:MM-HH:MM,HH:MM-HH:MM")
            start_raw, end_raw = [part.strip() for part in item.split("-", 1)]
            start = self._parse_menu_time_value(start_raw)
            end = self._parse_menu_time_value(end_raw)
            ranges.append({"start": start, "end": end})
        return ranges

    def _parse_menu_peak_ranges(self, raw: str) -> List[Dict[str, Any]]:
        text = raw.strip()
        if not text or text.casefold() in {"none", "clear", "off"}:
            return []
        peaks: List[Dict[str, Any]] = []
        for chunk in text.split(","):
            item = chunk.strip()
            if "=" not in item or "-" not in item:
                raise ValueError("Use HH:MM-HH:MM=MINUTES,HH:MM-HH:MM=MINUTES")
            range_part, interval_part = [part.strip() for part in item.split("=", 1)]
            start_raw, end_raw = [part.strip() for part in range_part.split("-", 1)]
            start = self._parse_menu_time_value(start_raw)
            end = self._parse_menu_time_value(end_raw)
            interval = max(1, int(interval_part))
            peaks.append({"start": start, "end": end, "post_interval_minutes": interval})
        return peaks

    def _parse_admin_menu_input(self, raw: str, parser: str) -> Any:
        text = raw.strip()
        if parser == "positive_int":
            value = int(text)
            if value < 1:
                raise ValueError("Send a number of at least 1.")
            return value
        if parser == "nonneg_int":
            value = int(text)
            if value < 0:
                raise ValueError("Send 0 or a positive number.")
            return value
        if parser == "time":
            return self._parse_menu_time_value(text)
        if parser == "timezone":
            if not text:
                raise ValueError("Timezone cannot be empty.")
            return text
        if parser == "channel_username":
            if not text:
                raise ValueError("Channel username cannot be empty.")
            if not text.startswith("@"):
                text = "@" + text
            return text
        if parser == "text":
            if not text:
                raise ValueError("Text cannot be empty.")
            return text
        if parser == "ranges":
            return self._parse_menu_ranges(text)
        if parser == "peak_ranges":
            return self._parse_menu_peak_ranges(text)
        raise ValueError(f"Unknown parser: {parser}")

    def _save_admin_menu_value(self, field: str, value: Any) -> str:
        if field == "default_channel_username":
            self.config.set_default_channel(str(value))
            field_names = ["channels"]
            saved = self._persist_runtime_config_fields(field_names)
            saved_text = "saved" if saved else "changed for this running bot only"
            return f"✅ Saved channel destination ({saved_text})."
        if field == "caption_mode":
            self.config.caption_mode = str(value or "template").strip().lower()
            saved = self._persist_runtime_config_fields(["caption_mode"])
            saved_text = "saved" if saved else "changed for this running bot only"
            return f"✅ Caption mode: {self._caption_mode_label(self.config.caption_mode)} ({saved_text})."
        setattr(self.config, field, value)
        field_names = [field]
        if field == "weekly_schedule_enabled":
            field_names = ["weekly_schedule_enabled", "weekly_schedule"]
        saved = self._persist_runtime_config_fields(field_names)
        saved_text = "saved" if saved else "changed for this running bot only"
        return f"✅ Saved {field} ({saved_text})."

    def _save_weekly_rule_value(self, rule_key: str, field: str, value: Any) -> str:
        schedule = copy.deepcopy(getattr(self.config, "weekly_schedule", {}) or {})
        rule = dict(schedule.get(rule_key, {}))
        if field in {"quiet_hours", "peak_hours"} and not value:
            rule.pop(field, None)
        else:
            rule[field] = value
        if not rule:
            schedule.pop(rule_key, None)
        else:
            schedule[rule_key] = rule
        self.config.weekly_schedule = schedule
        saved = self._persist_runtime_config_fields(["weekly_schedule", "weekly_schedule_enabled"])
        saved_text = "saved" if saved else "changed for this running bot only"
        return f"✅ Saved weekly rule {rule_key} ({saved_text})."

    def _clear_weekly_rule(self, rule_key: str) -> str:
        schedule = copy.deepcopy(getattr(self.config, "weekly_schedule", {}) or {})
        existed = rule_key in schedule
        schedule.pop(rule_key, None)
        self.config.weekly_schedule = schedule
        saved = self._persist_runtime_config_fields(["weekly_schedule", "weekly_schedule_enabled"])
        saved_text = "saved" if saved else "changed for this running bot only"
        if not existed:
            return "⚠️ That weekly rule was already empty."
        return f"🧹 Cleared weekly rule {rule_key} ({saved_text})."

    def _admin_menu_page(self, page: str, notice: str = "") -> tuple[str, Dict[str, Any]]:
        page = (page or "main").strip().lower()

        rows = [
            [
                self._menu_button("⏭️ Skip", "menu:act:main:skip"),
                self._menu_button("✅ Post Now", "menu:act:main:approve"),
            ],
            [
                self._menu_button("🐾 New Post", "menu:act:main:new_post"),
            ],
        ]
        return self._admin_menu_home_text(notice), self.telegram.create_inline_keyboard(rows)

    def handle_admin_menu_callback(self, callback: Dict[str, Any]) -> None:
        """Handle an admin menu callback by editing the current menu message."""
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        from_user = callback.get("from") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not chat_id or not isinstance(message_id, int):
            return

        parts = data.split(":")
        text = ""
        keyboard: Dict[str, Any]
        try:
            if len(parts) >= 3 and parts[1] == "page":
                text, keyboard = self._admin_menu_page("main")
            elif len(parts) >= 4 and parts[1] == "cmd":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="This menu now only supports Post Now, Skip, and New Post.",
                )
            elif len(parts) >= 4 and parts[1] == "act":
                action = parts[3]
                if action not in {"approve", "skip", "new_post"}:
                    notice = "This menu now only supports Post Now, Skip, and New Post."
                else:
                    notice = self._run_menu_action(action, from_user)
                text, keyboard = self._admin_menu_page("main", notice=notice)
            elif len(parts) >= 4 and parts[1] == "tog":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="Settings buttons were removed from the simplified menu.",
                )
            elif len(parts) >= 3 and parts[1] == "capmode":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="Caption controls were removed from the simplified menu.",
                )
            elif len(parts) >= 5 and parts[1] == "edit":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="Text-edit controls were removed from the simplified menu.",
                )
            elif len(parts) >= 4 and parts[1] == "wtog":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="Weekly controls were removed from the simplified menu.",
                )
            elif len(parts) >= 5 and parts[1] == "wedit":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="Weekly edit controls were removed from the simplified menu.",
                )
            elif len(parts) >= 3 and parts[1] == "wclear":
                text, keyboard = self._admin_menu_page(
                    "main",
                    notice="Weekly clear controls were removed from the simplified menu.",
                )
            else:
                text, keyboard = self._admin_menu_page("main", notice="⚠️ Unknown menu button.")
        except Exception as e:
            log(f"Admin menu callback failed: {e}", "ERROR")
            text = f"⚠️ Menu action failed\n\n{e}"
            keyboard = self._menu_nav_keyboard("main")

        self.telegram.edit_message_text(
            chat_id,
            message_id,
            self._format_menu_message_html(text),
            reply_markup=keyboard,
        )

    def _log_status(self, level: str = "DEBUG", force: bool = False) -> None:
        """Emit a concise live status line at a controlled rate."""
        now_ts = time.time()
        if not force and (now_ts - self._status_last_log_ts) < self._status_interval_seconds:
            return
        self._status_last_log_ts = now_ts

        step = self._status.get("step", "-")
        source = self._status.get("source", "-")
        progress = self._status.get("progress", "-")
        result = self._status.get("result", "-")
        event = self._status.get("event", "-")
        pending = self._status_pending_text()
        next_in = self._status_next_text()
        last_post = self._status_last_post_text()
        errors = self._status_errors_today

        progress_part = f"{source} ({progress})" if source != "-" else progress
        log(
            f"Status | step={step} | src={progress_part} | result={result} | pending={pending} | next={next_in} | last={last_post} | errors={errors} | {event}",
            level,
        )

    def _status_update(
        self,
        *,
        step: Optional[str] = None,
        source: Optional[str] = None,
        progress: Optional[str] = None,
        result: Optional[str] = None,
        event: Optional[str] = None,
        level: str = "DEBUG",
        force: bool = False,
    ) -> None:
        """Update status fields and optionally emit a status line."""
        if step is not None:
            self._status["step"] = step
        if source is not None:
            self._status["source"] = source
        if progress is not None:
            self._status["progress"] = progress
        if result is not None:
            self._status["result"] = result
        if event is not None:
            self._status["event"] = event

        if level.upper() == "ERROR":
            self._status_errors_today += 1
            try:
                reason = event or result or step or "Runtime error"
                self.state.record_failure(str(reason), self._local_date_key())
            except Exception:
                pass

        self._log_status(level=level, force=force)

    def _emergency_thresholds(self) -> Dict[str, int]:
        """Return configured emergency pause thresholds by failure category."""
        defaults = {
            "reddit": 5,
            "telegram": 3,
            "download": 6,
            "empty_feed": 4,
        }
        raw = getattr(self.config, "emergency_pause_thresholds", {}) or {}
        thresholds = dict(defaults)
        if isinstance(raw, dict):
            for category in thresholds:
                try:
                    thresholds[category] = max(0, int(raw.get(category, thresholds[category]) or 0))
                except (TypeError, ValueError):
                    pass
        return thresholds

    def _record_emergency_failure(self, category: str, reason: str) -> None:
        """Record a categorized runtime failure and pause if the threshold is reached."""
        if not bool(getattr(self.config, "emergency_pause_enabled", True)):
            return

        category_key = str(category or "runtime").strip().lower().replace("-", "_")
        thresholds = self._emergency_thresholds()
        threshold = int(thresholds.get(category_key, 0) or 0)
        if threshold <= 0:
            return

        window_minutes = int(getattr(self.config, "emergency_pause_window_minutes", 30) or 30)
        window_minutes = max(1, window_minutes)
        counts = self.state.record_emergency_failure(category_key, reason, window_minutes)
        count = int(counts.get(category_key, 0) or 0)

        if count < threshold:
            self.state.save()
            return

        already_paused = self.state.get_emergency_pause()
        if already_paused:
            self.state.save()
            return

        self._activate_emergency_pause(
            category=category_key,
            reason=reason,
            count=count,
            threshold=threshold,
            window_minutes=window_minutes,
        )

    def _activate_emergency_pause(
        self,
        *,
        category: str,
        reason: str,
        count: int,
        threshold: int,
        window_minutes: int,
    ) -> None:
        """Pause posting immediately and notify the admin once."""
        self.state.set_emergency_pause(
            category=category,
            reason=reason,
            count=count,
            threshold=threshold,
            window_minutes=window_minutes,
        )
        self.state.save()

        log(
            f"Emergency pause triggered: {category} failures {count}/{threshold} in {window_minutes}m",
            "ERROR",
        )

        if not bool(getattr(self.config, "emergency_pause_notify_admin", True)):
            return
        if not self.telegram:
            return

        try:
            self._send_admin_message_html(
                "Emergency pause triggered\n\n"
                f"Category: {category}\n"
                f"Failures: {count}/{threshold} in {window_minutes}m\n"
                f"Reason: {str(reason or 'Unknown failure')[:300]}\n\n"
                "Posting is paused. Use /emergency to inspect it or /resume after fixing the issue.",
                disable_notification=False,
            )
        except Exception as e:
            log(f"Could not notify admin about emergency pause: {e}", "WARN")

    def _pause_for_reddit_configuration_issue(self, reason: str) -> None:
        """Stop the bot on a permanent Reddit configuration block instead of retrying forever."""
        window_minutes = max(1, int(getattr(self.config, "emergency_pause_window_minutes", 30) or 30))
        self.state.clear_reddit_backoff()
        try:
            self.state.record_emergency_failure("reddit", reason, window_minutes)
        except Exception:
            pass

        if not self.state.get_emergency_pause():
            self._activate_emergency_pause(
                category="reddit",
                reason=reason,
                count=1,
                threshold=1,
                window_minutes=window_minutes,
            )
        else:
            self.state.save()

    def _get_reddit_backoff_deadline(self) -> Optional[datetime]:
        """Return the active Reddit backoff deadline, if one is stored."""
        backoff = self.state.get_reddit_backoff()
        if not backoff:
            return None

        try:
            return datetime.fromisoformat(backoff)
        except Exception as e:
            log(f"Error checking backoff: {e}", "ERROR")
            self.state.clear_reddit_backoff()
            self.state.save()
            return None

    def _is_reddit_backoff_active(self, *, log_remaining: bool = True) -> bool:
        """Check whether Reddit fetches should be paused."""
        until = self._get_reddit_backoff_deadline()
        if not until:
            return False

        if now_utc() < until:
            if log_remaining:
                remaining = max(0, int((until - now_utc()).total_seconds()))
                log(f"Reddit backoff active for {format_seconds(remaining)} more")
            return True

        self.state.clear_reddit_backoff()
        self.state.save()
        return False

    def _set_reddit_backoff(
        self,
        *,
        reason: str,
        min_seconds: int,
        max_seconds: int,
        error: Optional[Exception] = None,
        emergency_category: str = "reddit",
    ) -> None:
        """Pause Reddit fetches after rate limits or repeated empty attempts."""
        retry_after_seconds: Optional[int] = None
        response = getattr(error, "response", None)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    retry_after_seconds = max(0, int(float(retry_after)))
                except ValueError:
                    retry_after_seconds = None

        backoff_seconds = retry_after_seconds or random.randint(min_seconds, max_seconds)
        until = now_utc() + timedelta(seconds=backoff_seconds)
        self.state.set_reddit_backoff(until.isoformat())
        self.state.save()
        log(f"{reason}; pausing Reddit fetches for {format_seconds(backoff_seconds)}", "WARN")
        self._record_emergency_failure(emergency_category, reason)

    def _clear_reddit_backoff_on_success(self) -> None:
        """Clear any stale Reddit backoff after a successful fetch path."""
        if not self.state.get_reddit_backoff():
            return
        self.state.clear_reddit_backoff()
        self.state.save()
        log("Reddit fetch recovered; cleared Reddit backoff", "INFO")

    def _recent_media_preference(self, lookback: int = 12) -> Optional[str]:
        """Prefer the media type that is behind in recent successful posts."""
        try:
            history = self.state.get_history(lookback)
        except Exception:
            return None

        image_like = 0
        video_count = 0
        for item in history:
            media_type = str(item.get("type") or "").strip().lower()
            if media_type == "video":
                video_count += 1
            elif media_type in {"image", "gallery"}:
                image_like += 1

        if image_like <= 0 and video_count <= 0:
            return None
        if video_count < image_like:
            return "video"
        if image_like < video_count:
            return "image"
        return None

    def _filter_candidate_posts(
        self,
        subreddit: str,
        parsed_posts: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Filter parsed posts and fall back to older unseen posts when needed."""
        rule = self.config.get_effective_subreddit_rule(subreddit)
        max_post_age_hours = int(rule.get("max_post_age_hours", self.config.max_post_age_hours) or 0)
        skip_nsfw = bool(rule.get("skip_nsfw", self.config.skip_nsfw))
        posts = self.filter.filter_posts(parsed_posts, verbose=False)
        used_age_fallback = False

        if not posts and parsed_posts and max_post_age_hours > 0:
            posts = self.filter.filter_posts(
                parsed_posts,
                verbose=False,
                ignore_age=True,
            )
            if posts:
                used_age_fallback = True
                log(
                    f"No fresh posts in r/{subreddit}; using {len(posts)} older posts that passed other filters",
                    "INFO",
                )

        if not posts and parsed_posts and skip_nsfw and all(bool(p.get("nsfw", False)) for p in parsed_posts):
            log(
                f"All parsed posts in r/{subreddit} are NSFW. Disable 'Skip NSFW content' in Settings to allow them.",
                "INFO",
            )

        return posts, used_age_fallback

    def _fetch_subreddit_candidates(
        self,
        subreddit: str,
        seen_ids: set[str],
        seen_urls: set[str],
        seen_signatures: set[str],
        preferred_type: Optional[str] = None,
        block_crossposts: bool = True,
    ) -> tuple[List[Dict[str, Any]], int, bool, int]:
        """
        Fetch candidates for a subreddit and widen the scan window when needed.

        Returns:
            posts, parsed_count, used_age_fallback, fetch_limit_used
        """
        parsed_count = 0
        fetch_limit_used = self.SEARCH_FETCH_LIMITS[0]
        used_age_fallback = False
        after: Optional[str] = None
        page_number = 1
        fallback_posts: List[Dict[str, Any]] = []
        fallback_parsed_count = 0
        fallback_used_age_fallback = False
        fallback_fetch_limit_used = fetch_limit_used

        def should_keep_scanning(posts: List[Dict[str, Any]]) -> bool:
            if not preferred_type:
                return False
            return posts and not any(p.get("type") == preferred_type for p in posts)

        for attempt, fetch_limit in enumerate(self.SEARCH_FETCH_LIMITS):
            fetch_limit_used = fetch_limit
            if attempt == 0:
                data = self.reddit.fetch_with_jitter(subreddit, limit=fetch_limit)
            else:
                previous_limit = self.SEARCH_FETCH_LIMITS[attempt - 1]
                log(
                    f"No usable unseen posts in the top {previous_limit} of r/{subreddit}; widening search to {fetch_limit}",
                    "INFO",
                )
                time.sleep(random.uniform(1.0, 2.0))
                data = self.reddit.fetch_subreddit_new(subreddit, limit=fetch_limit)

            if data is None:
                return [], 0, False, fetch_limit_used
            self._clear_reddit_backoff_on_success()

            children = data.get("data", {}).get("children", [])
            after = data.get("data", {}).get("after")
            parsed_posts = self.reddit.parse_posts(
                data,
                seen_ids,
                seen_urls,
                seen_signatures,
                block_crossposts=block_crossposts,
            )
            parsed_count = len(parsed_posts)

            log(
                f"Parsed {parsed_count} unseen usable posts from r/{subreddit} (top {fetch_limit})",
                "DEBUG",
            )

            posts, used_age_fallback = self._filter_candidate_posts(subreddit, parsed_posts)
            if posts:
                if should_keep_scanning(posts):
                    if not fallback_posts:
                        fallback_posts = posts
                        fallback_parsed_count = parsed_count
                        fallback_used_age_fallback = used_age_fallback
                        fallback_fetch_limit_used = fetch_limit_used
                    log(
                        f"No {preferred_type} candidates in the newest {fetch_limit} of r/{subreddit}; scanning deeper",
                        "INFO",
                    )
                else:
                    return posts, parsed_count, used_age_fallback, fetch_limit_used

            if len(children) < fetch_limit:
                break

        max_history_pages = 3 if preferred_type else 8
        while after:
            page_number += 1
            fetch_limit_used = page_number * 100
            log(
                f"No usable unseen posts in the newest {fetch_limit_used - 100} of r/{subreddit}; scanning older history page {page_number}",
                "INFO",
            )
            time.sleep(random.uniform(1.0, 2.0))
            data = self.reddit.fetch_subreddit_new(
                subreddit,
                limit=100,
                after=after,
            )

            if data is None:
                break
            self._clear_reddit_backoff_on_success()

            children = data.get("data", {}).get("children", [])
            after = data.get("data", {}).get("after")
            if not children:
                break

            parsed_posts = self.reddit.parse_posts(
                data,
                seen_ids,
                seen_urls,
                seen_signatures,
                block_crossposts=block_crossposts,
            )
            parsed_count = len(parsed_posts)

            log(
                f"Parsed {parsed_count} unseen usable posts from r/{subreddit} (history page {page_number})",
                "DEBUG",
            )

            posts, used_age_fallback = self._filter_candidate_posts(subreddit, parsed_posts)
            if posts:
                if should_keep_scanning(posts):
                    if not fallback_posts:
                        fallback_posts = posts
                        fallback_parsed_count = parsed_count
                        fallback_used_age_fallback = used_age_fallback
                        fallback_fetch_limit_used = fetch_limit_used
                    log(
                        f"Still no {preferred_type} candidates by history page {page_number} in r/{subreddit}; continuing deeper",
                        "INFO",
                    )
                else:
                    return posts, parsed_count, used_age_fallback, fetch_limit_used

            if preferred_type and page_number >= max_history_pages:
                log(
                    f"Reached history scan cap for forced {preferred_type} search in r/{subreddit}; using other media if needed",
                    "WARN",
                )
                break

        if fallback_posts:
            return (
                fallback_posts,
                fallback_parsed_count,
                fallback_used_age_fallback,
                fallback_fetch_limit_used,
            )

        return [], parsed_count, used_age_fallback, fetch_limit_used

    def initialize(self) -> bool:
        """Initialize all components"""
        log("Initializing bot components...", "DEBUG")

        # Load config
        if not self._config_preloaded and not self.config.load():
            log("No configuration found, please run setup first", "ERROR")
            return False

        # Validate config
        valid, error = self.config.validate()
        if not valid:
            log(f"Configuration error: {error}", "ERROR")
            return False

        # Load state
        self.state.load(quiet=True)
        has_reddit_oauth = getattr(self.config, "has_reddit_oauth_credentials", None)
        if callable(has_reddit_oauth) and not has_reddit_oauth() and self.state.get_reddit_backoff():
            self.state.clear_reddit_backoff()
            self.state.save()
            log("Cleared persisted Reddit backoff because RSS fallback is enabled", "INFO")
        backfilled_signatures = self.state.backfill_seen_signatures_from_cache()
        if backfilled_signatures:
            self.state.save()
            log(
                f"Recovered {backfilled_signatures} content signatures from cached state",
                "DEBUG",
            )

        # Initialize handlers
        self.telegram = TelegramHandler(self.config.bot_token)
        self.reddit = RedditHandler(
            self.config.user_agent,
            reddit_client_id=getattr(self.config, "reddit_client_id", ""),
            reddit_client_secret=getattr(self.config, "reddit_client_secret", ""),
            domain_downloaders_enabled=getattr(self.config, "domain_downloaders_enabled", True),
            imgur_album_downloads_enabled=getattr(self.config, "imgur_album_downloads_enabled", True),
            html_media_resolver_enabled=getattr(self.config, "html_media_resolver_enabled", True),
        )
        self.media = MediaHandler(
            self.config.user_agent,
            self.config.max_download_mb,
            domain_downloaders_enabled=getattr(self.config, "domain_downloaders_enabled", True),
            imgur_album_downloads_enabled=getattr(self.config, "imgur_album_downloads_enabled", True),
            html_media_resolver_enabled=getattr(self.config, "html_media_resolver_enabled", True),
        )
        self.filter = ContentFilter(self.config)
        self.scheduler = Scheduler(self.config)
        try:
            self.traffic = TrafficService(self.config)
            self.traffic.initialize()
        except Exception as e:
            self.traffic = None
            log(f"Traffic analytics disabled: {e}", "WARN")
        self.commands = CommandHandler(
            self.config,
            self.state,
            self.scheduler,
            traffic_service=self.traffic,
            telegram_handler=self.telegram,
            reddit_handler=self.reddit,
            media_handler=self.media,
        )

        log("All components initialized", "DEBUG")
        if callable(has_reddit_oauth) and has_reddit_oauth():
            log("Reddit API mode: OAuth app-only", "INFO")
        else:
            log(
                "Reddit API mode: anonymous access with RSS media fallback when Reddit blocks JSON. OAuth is optional but more reliable.",
                "INFO",
            )
        return True

    def bootstrap(self) -> bool:
        """Bootstrap bot (verify tokens, webhooks, etc.)"""
        log("Bootstrapping bot...", "DEBUG")

        # Test bot token
        valid, username = self.telegram.test_bot_token()
        if not valid:
            log(f"Bot token check failed: {username}", "ERROR")
            return False

        log(f"Bot token valid: @{username}", "SUCCESS")

        # Delete webhook
        self.telegram.delete_webhook()

        # Test admin messaging
        admin_ids = self.config.get_admin_user_ids()
        if not admin_ids:
            log("Cannot message admin: no admin user IDs configured", "ERROR")
            return False
        valid, error = self.telegram.test_can_message_admin(admin_ids[0])
        if not valid:
            log(f"Cannot message admin: {error}", "ERROR")
            log("Make sure you've started a chat with the bot first!", "ERROR")
            return False

        log("Admin messaging test successful", "SUCCESS")

        # Fail fast on permanent Reddit access problems instead of discovering them later in the main loop.
        try:
            subreddit = next(
                (str(item or "").strip() for item in getattr(self.config, "subreddits", []) if str(item or "").strip()),
                "",
            )
            if subreddit:
                self.reddit.fetch_subreddit_new(subreddit, limit=1, timeout=12)
                self._clear_reddit_backoff_on_success()
                mode = "OAuth app-only" if self.reddit.uses_oauth() else "legacy anonymous"
                log(f"Reddit API preflight successful for r/{subreddit} ({mode})", "SUCCESS")
        except RedditConfigurationError as e:
            if self.reddit.uses_oauth():
                log(f"Reddit configuration error: {e}", "ERROR")
                return False
            log(f"Reddit preflight blocked in legacy mode: {e}", "WARN")
            log(
                "Continuing startup in legacy mode. Reddit fetches may back off or pause until OAuth credentials are configured.",
                "WARN",
            )
        except Exception as e:
            log(f"Reddit preflight warning: {e}", "WARN")

        # Auto-configure bot command menu.
        try:
            private_command_menu = [
                {"command": "start", "description": "Start the bot"},
                {"command": "help", "description": "Show help"},
            ]
            admin_command_menu = [
                {"command": "start", "description": "Welcome message"},
                {"command": "menu", "description": "Open Post Now / Skip / New Post controls"},
                {"command": "help", "description": "This help message"},
                {"command": "skip", "description": "Skip the current pending post"},
            ]
            self.telegram.delete_my_commands(scope={"type": "default"})
            self.telegram.delete_my_commands(scope={"type": "all_group_chats"})
            self.telegram.delete_my_commands(scope={"type": "all_chat_administrators"})
            self.telegram.set_my_commands(
                private_command_menu,
                scope={"type": "all_private_chats"},
            )
            for admin_id in self.config.get_admin_user_ids():
                self.telegram.set_my_commands(
                    admin_command_menu,
                    scope={"type": "chat", "chat_id": admin_id},
                )
        except Exception as e:
            log(f"Could not configure bot command menu: {e}", "WARN")

        # Test channel access (optional, non-blocking)
        channel = self.config.get_default_channel()
        if channel:
            # We can't easily test channel access without bot_id, skip for now
            log(f"Will post to channel: {channel}", "DEBUG")

        log("Bootstrap complete", "SUCCESS")
        return True

    def make_caption(self, post: Dict[str, Any]) -> str:
        """
        Generate caption for post based on caption mode.

        Args:
            post: Post dictionary

        Returns:
            Caption string
        """
        caption_mode = getattr(self.config, "caption_mode", "template")

        title = (post.get("title") or "").strip()
        subreddit = (post.get("subreddit") or "").strip()
        author = (post.get("author") or "").strip()
        body = (post.get("selftext") or "").strip()
        permalink = (post.get("permalink") or "").strip()
        reddit_url = f"https://reddit.com{permalink}" if permalink else ""

        def apply_template(template: str) -> str:
            """Apply placeholder replacement for template-based captions."""
            body_excerpt = body[:500].strip()
            replacements = {
                "{title}": title,
                "{subreddit}": subreddit,
                "{author}": author,
                "{body}": body_excerpt,
                "{permalink}": permalink,
                "{reddit_url}": reddit_url,
                "{url}": post.get("url", ""),
            }
            result = template or ""
            for key, value in replacements.items():
                result = result.replace(key, value)
            return result.strip()

        def render_mode(
            mode: str,
            *,
            template: Optional[str] = None,
            footer_template: Optional[str] = None,
        ) -> str:
            """Render one caption mode or variant."""
            mode = str(mode or "template").strip().lower()

            if mode == "source":
                return title
            if mode == "source_plus_footer":
                footer_source = (
                    footer_template
                    if footer_template is not None
                    else getattr(self.config, "caption_footer_template", "")
                )
                footer = apply_template(footer_source)
                if title and footer:
                    return f"{title}\n\n{footer}"
                return title or footer
            if mode == "source_plus_body":
                if body:
                    body_excerpt = body[:700].strip()
                    return f"{title}\n\n{body_excerpt}" if title else body_excerpt
                return title
            if mode == "source_with_credit":
                credit = f"r/{subreddit}" if subreddit else ""
                if title and credit:
                    return f"{title}\n\n{credit}"
                return title or credit
            if mode == "credit_only":
                return f"r/{subreddit}" if subreddit else ""
            if mode == "none":
                return ""

            return apply_template(template if template is not None else self.config.caption_template)

        def choose_caption_variant(variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            """Select the next caption variant based on successful post count."""
            if not variants:
                return None
            try:
                total_posts = int(self.state.get_stats().get("total_posts", 0) or 0)
            except Exception:
                total_posts = 0
            return variants[total_posts % len(variants)]

        def render_variant(variant: Dict[str, Any]) -> str:
            """Render a configured caption variant."""
            return render_mode(
                str(variant.get("mode") or "template"),
                template=variant.get("template"),
                footer_template=variant.get("footer_template"),
            )

        rule_variants: List[Dict[str, Any]] = []
        get_rule_variants = getattr(self.config, "get_subreddit_caption_variants", None)
        if callable(get_rule_variants):
            rule_variants = get_rule_variants(subreddit)

        rule_template = ""
        get_rule_template = getattr(self.config, "get_subreddit_caption_template", None)
        if callable(get_rule_template):
            rule_template = get_rule_template(subreddit)

        global_variants = list(getattr(self.config, "caption_variants", []) or [])
        selected_variant = choose_caption_variant(rule_variants)
        if not selected_variant and not rule_template and caption_mode == "variants":
            selected_variant = choose_caption_variant(global_variants)

        if selected_variant:
            caption = render_variant(selected_variant)
        elif rule_template:
            caption = apply_template(rule_template)
        else:
            caption = render_mode(caption_mode)

        rule_footer = apply_template(self.config.get_subreddit_caption_footer(subreddit))
        if rule_footer:
            caption = f"{caption}\n\n{rule_footer}" if caption else rule_footer

        # Add hashtag if enabled and we have a caption
        if caption and self.config.add_subreddit_hashtag and subreddit:
            hashtag = f"#{subreddit}"
            caption = f"{caption}\n{hashtag}"

        return caption[:1024]  # Telegram limit

    def make_preview_caption(self, post: Dict[str, Any]) -> str:
        """
        Generate preview caption with metadata.

        Args:
            post: Post dictionary

        Returns:
            Preview caption string
        """
        lines = []

        # Title
        title = post.get("title", "")
        if title:
            lines.append(f"📝 {title[:150]}")

        # Metadata
        meta_parts = []
        meta_parts.append(f"r/{post.get('subreddit', '?')}")

        upvotes = post.get("upvotes", 0)
        if upvotes > 0:
            meta_parts.append(f"⬆️ {upvotes}")

        comments = post.get("num_comments", 0)
        if comments > 0:
            meta_parts.append(f"💬 {comments}")

        if post.get("type") == "gallery":
            gallery_count = post.get("gallery_count")
            gallery_items = post.get("gallery_items")
            has_deferred_album = isinstance(gallery_items, list) and any(
                isinstance(item, dict) and str(item.get("source") or "") in {"imgur_album", "external_album"}
                for item in gallery_items
            )
            if not gallery_count and isinstance(gallery_items, list) and not has_deferred_album:
                gallery_count = len(gallery_items)
            if gallery_count:
                meta_parts.append(f"gallery {gallery_count}")
        elif post.get("type") == "video" and isinstance(post.get("video_info"), dict):
            video_info = post["video_info"]
            video_bits = []
            duration = video_info.get("duration_seconds")
            if isinstance(duration, (int, float)) and duration > 0:
                video_bits.append(f"{int(round(duration))}s")
            orientation = str(video_info.get("orientation") or "").strip()
            if orientation and orientation != "unknown":
                video_bits.append(orientation)
            has_audio = video_info.get("has_audio")
            if has_audio is True:
                video_bits.append("audio")
            elif has_audio is False:
                video_bits.append("silent")
            if video_bits:
                meta_parts.append("video " + ", ".join(video_bits))

        # Post age
        created_utc = post.get("created_utc", 0)
        if created_utc:
            age_hours = self.reddit.calculate_post_age_hours(created_utc)
            if age_hours < 1:
                meta_parts.append(f"🕐 {int(age_hours * 60)}m ago")
            else:
                meta_parts.append(f"🕐 {int(age_hours)}h ago")

        lines.append(" • ".join(meta_parts))

        return "\n".join(lines)

    def _image_quality_options(self) -> Dict[str, Any]:
        """Return configured image quality options for single images and galleries."""
        return {
            "enabled": bool(getattr(self.config, "image_quality_rules_enabled", True)),
            "min_height": int(getattr(self.config, "min_image_height", 0) or 0),
            "aspect_ratio_min": float(getattr(self.config, "image_aspect_ratio_min", 0.20) or 0.20),
            "aspect_ratio_max": float(getattr(self.config, "image_aspect_ratio_max", 5.00) or 5.00),
            "blur_filter_enabled": bool(getattr(self.config, "image_blur_filter_enabled", False)),
            "blur_score_min": float(getattr(self.config, "image_blur_score_min", 35.0) or 35.0),
            "screenshot_filter_enabled": bool(getattr(self.config, "image_screenshot_filter_enabled", False)),
            "text_heavy_filter_enabled": bool(getattr(self.config, "image_text_heavy_filter_enabled", False)),
            "text_heavy_max_edge_density": float(
                getattr(self.config, "image_text_heavy_max_edge_density", 0.18) or 0.18
            ),
        }

    def _validate_gallery_media_items(
        self,
        media_items: List[Dict[str, Any]],
    ) -> tuple[bool, List[Dict[str, Any]], str]:
        """Drop downloaded gallery items that fail image quality rules."""
        min_items = max(2, int(getattr(self.config, "min_gallery_items", 2) or 2))
        accepted: List[Dict[str, Any]] = []
        rejected_reasons: List[str] = []

        for item in media_items:
            data = item.get("bytes") if isinstance(item, dict) else None
            if not isinstance(data, (bytes, bytearray)):
                rejected_reasons.append("missing image bytes")
                continue

            acceptable, reason = self.media.is_image_quality_acceptable(
                bytes(data),
                self.config.min_image_width,
                **self._image_quality_options(),
            )
            if acceptable:
                accepted.append(item)
            else:
                rejected_reasons.append(reason)
                log(f"Gallery item quality check failed: {reason}", "WARN")

        if len(accepted) < min_items:
            detail = "; ".join(rejected_reasons[:3]) if rejected_reasons else "not enough items"
            return False, accepted, f"Only {len(accepted)} gallery item(s) passed; need {min_items}. {detail}"

        return True, accepted, ""

    def send_preview_to_admin(self, post: Dict[str, Any], media_bytes: Any) -> Optional[int]:
        """
        Send preview to admin for approval.

        Args:
            post: Post dictionary
            media_bytes: Downloaded media bytes

        Returns:
            Message ID of preview, or None if failed
        """
        caption = self.make_preview_caption(post)
        keyboard = self.telegram.create_inline_keyboard(
            [
                [
                    {"text": "Skip", "callback_data": "skip"},
                    {"text": "Post Now", "callback_data": "approve"},
                ],
            ]
        )

        try:
            admin_ids = self.config.get_admin_user_ids()
            if not admin_ids:
                raise ValueError("No admin user IDs configured")
            first_message_id: Optional[int] = None
            preview_msg_ids: Dict[str, int] = {}
            if post["type"] == "gallery":
                if not isinstance(media_bytes, list) or len(media_bytes) < 2:
                    raise ValueError("Gallery preview needs at least two media items")
                delivered = False
                for admin_id in admin_ids:
                    try:
                        self.telegram.send_media_group(
                            admin_id,
                            media_bytes,
                            caption=caption,
                            disable_notification=True,
                        )
                        result = self.telegram.send_message(
                            admin_id,
                            self._format_admin_message_html(f"🖼️ Gallery preview ready\n\n{len(media_bytes)} item(s)."),
                            reply_markup=keyboard,
                            disable_notification=True,
                            parse_mode="HTML",
                        )
                        delivered = True
                        msg_id = result["result"]["message_id"]
                        preview_msg_ids[str(admin_id)] = msg_id
                        if first_message_id is None:
                            first_message_id = msg_id
                    except Exception as e:
                        log(f"Failed to send gallery preview to admin {admin_id}: {e}", "WARN")
                post["preview_msg_ids"] = preview_msg_ids
                return first_message_id if delivered else None

            if post["type"] == "image":
                delivered = False
                for admin_id in admin_ids:
                    try:
                        if self.telegram.looks_like_image(media_bytes):
                            try:
                                result = self.telegram.send_photo(
                                    admin_id,
                                    media_bytes,
                                    caption=caption,
                                    reply_markup=keyboard,
                                    disable_notification=True,
                                )
                            except Exception as e:
                                log(f"sendPhoto failed for admin {admin_id}, trying document: {e}", "WARN")
                                result = self.telegram.send_document(
                                    admin_id,
                                    media_bytes,
                                    filename="preview.jpg",
                                    caption=caption,
                                    reply_markup=keyboard,
                                    disable_notification=True,
                                )
                        else:
                            result = self.telegram.send_document(
                                admin_id,
                                media_bytes,
                                filename="preview.jpg",
                                caption=caption,
                                reply_markup=keyboard,
                                disable_notification=True,
                            )
                        delivered = True
                        msg_id = result["result"]["message_id"]
                        preview_msg_ids[str(admin_id)] = msg_id
                        if first_message_id is None:
                            first_message_id = msg_id
                    except Exception as e:
                        log(f"Failed to send image preview to admin {admin_id}: {e}", "WARN")
                post["preview_msg_ids"] = preview_msg_ids
                return first_message_id if delivered else None

            else:  # video
                delivered = False
                for admin_id in admin_ids:
                    try:
                        result = self.telegram.send_video(
                            admin_id,
                            media_bytes,
                            caption=caption,
                            reply_markup=keyboard,
                            disable_notification=True,
                            supports_streaming=True,
                        )
                        delivered = True
                        msg_id = result["result"]["message_id"]
                        preview_msg_ids[str(admin_id)] = msg_id
                        if first_message_id is None:
                            first_message_id = msg_id
                    except Exception as e:
                        log(f"Failed to send video preview to admin {admin_id}: {e}", "WARN")
                post["preview_msg_ids"] = preview_msg_ids
                return first_message_id if delivered else None

        except Exception as e:
            log(f"Failed to send preview to admin: {e}", "ERROR")
            self._record_emergency_failure("telegram", f"admin preview failed: {e}")
            return None

    def _fallback_reactions(self) -> List[Dict[str, Any]]:
        """Return a safe fallback pool when the chat allows all standard emoji reactions."""
        allowed = []
        for emoji in getattr(self.config, "auto_reaction_emojis", []):
            if isinstance(emoji, str) and emoji.strip():
                allowed.append({"type": "emoji", "emoji": emoji.strip()})
        return allowed

    def _pick_random_reaction(self, channel: str) -> Optional[Dict[str, Any]]:
        """Pick one random reaction allowed in the target chat."""
        fallback = self._fallback_reactions()

        try:
            chat = self.telegram.get_chat(channel).get("result", {})
        except Exception as e:
            log(f"Could not load chat reactions, using fallback list: {e}", "WARN")
            return random.choice(fallback) if fallback else None

        if chat.get("max_reaction_count", 1) < 1:
            log(f"Reactions are disabled for {channel}", "DEBUG")
            return None

        available = chat.get("available_reactions")
        if available in (None, "all"):
            return random.choice(fallback) if fallback else None

        allowed = []
        if isinstance(available, list):
            for item in available:
                if not isinstance(item, dict):
                    continue

                reaction_type = item.get("type")
                if reaction_type == "emoji" and item.get("emoji"):
                    allowed.append({"type": "emoji", "emoji": item["emoji"]})
                elif reaction_type == "custom_emoji" and item.get("custom_emoji_id"):
                    allowed.append(
                        {
                            "type": "custom_emoji",
                            "custom_emoji_id": item["custom_emoji_id"],
                        }
                    )

        return random.choice(allowed) if allowed else None

    def _maybe_add_random_reaction(self, channel: str, message_id: int) -> None:
        """Best-effort random reaction after a successful channel post."""
        if not getattr(self.config, "auto_reactions_enabled", True):
            return

        reaction = self._pick_random_reaction(channel)
        if not reaction:
            log(f"No usable reactions available for {channel}", "DEBUG")
            return

        try:
            self.telegram.set_message_reaction(channel, message_id, [reaction])
            reaction_name = reaction.get("emoji") or "custom emoji"
            log(f"Added random reaction {reaction_name} to post {message_id}", "DEBUG")
        except Exception as e:
            log(f"Could not add random reaction to post {message_id}: {e}", "WARN")

    def post_to_channel(self, post: Dict[str, Any], media_bytes: Any) -> bool:
        """
        Post media to channel.

        Args:
            post: Post dictionary
            media_bytes: Downloaded media bytes

        Returns:
            True if successful
        """
        channel = self.config.get_default_channel()
        if not channel:
            log("No channel configured", "ERROR")
            self._record_emergency_failure("telegram", "no channel configured")
            return False

        caption = self.make_caption(post)
        self._last_channel_post_message = None

        # Add Reddit link button if enabled
        reply_markup = None
        if self.config.add_reddit_link_button and post.get("permalink"):
            reddit_url = f"https://reddit.com{post['permalink']}"
            reply_markup = self.telegram.create_inline_keyboard([[{"text": "View on Reddit", "url": reddit_url}]])

        has_spoiler = bool(getattr(self.config, "spoiler_posts_enabled", False))
        recovery_enabled = self._auto_recovery_enabled()
        retry_count = (
            max(0, int(getattr(self.config, "auto_recovery_upload_retries", 2) or 0)) if recovery_enabled else 0
        )
        max_attempts = 1 + min(5, retry_count)
        retry_delay = max(
            0,
            min(60, int(getattr(self.config, "auto_recovery_retry_delay_seconds", 5) or 0)),
        )
        compress_on_retry = bool(getattr(self.config, "auto_recovery_compress_on_retry", True))

        current_media = media_bytes
        compressed_for_retry = False
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            try:
                result_message = self._send_channel_media_once(
                    channel,
                    post,
                    current_media,
                    caption,
                    reply_markup,
                    has_spoiler,
                )

                message_id = result_message.get("message_id")
                if isinstance(message_id, int):
                    self._maybe_add_random_reaction(channel, message_id)
                if isinstance(result_message, dict):
                    self._last_channel_post_message = result_message

                if attempt > 1:
                    self._record_recovery_event(
                        "upload_recovered",
                        f"Upload succeeded on attempt {attempt}/{max_attempts}",
                        category="upload",
                        level="info",
                        post=post,
                    )

                log(f"Posted to channel: {post['type']} from r/{post['subreddit']}", "SUCCESS")
                return True
            except Exception as e:
                last_error = str(e)
                final_attempt = attempt >= max_attempts
                log_level = "ERROR" if final_attempt else "WARN"
                log(
                    f"Channel upload attempt {attempt}/{max_attempts} failed: {last_error}",
                    log_level,
                )

                if not recovery_enabled:
                    break

                self._record_recovery_event(
                    "upload_failed" if final_attempt else "upload_retry",
                    f"Attempt {attempt}/{max_attempts}: {last_error}",
                    category="upload",
                    level="error" if final_attempt else "info",
                    post=post,
                )

                if final_attempt:
                    break

                if compress_on_retry and not compressed_for_retry:
                    compressed_for_retry = True
                    compressed, retry_media, compression_error = self._compress_media_for_retry(
                        post,
                        current_media,
                    )
                    if compressed:
                        current_media = retry_media
                        self._record_recovery_event(
                            "compressed_retry",
                            "Using compressed media for the next upload attempt",
                            category="upload",
                            level="info",
                            post=post,
                        )
                    else:
                        self._record_recovery_event(
                            "compression_skipped",
                            compression_error,
                            category="upload",
                            level="warn",
                            post=post,
                        )

                if retry_delay > 0:
                    time.sleep(retry_delay)

        log(f"Failed to post to channel: {last_error or 'unknown upload error'}", "ERROR")
        self._record_emergency_failure(
            "telegram",
            f"channel post failed: {last_error or 'unknown upload error'}",
        )
        return False

    def _download_media_for_post(self, post: Dict[str, Any]) -> tuple[bool, Optional[Any], Optional[str]]:
        """Download media for a post-like payload."""
        if post.get("type") == "gallery":
            return self.media.download_gallery(
                post.get("gallery_items", []),
                max_items=getattr(self.config, "max_gallery_items", 10),
                min_items=getattr(self.config, "min_gallery_items", 2),
            )

        is_reddit_video = post["type"] == "video" and (
            url_matches_domain(post["url"], "v.redd.it") or url_matches_domain(post["url"], "reddit.com")
        )

        if is_reddit_video:
            log("Reddit video detected during posting, using audio merge", "DEBUG")
            success, media_bytes, error = self.media.download_reddit_video_with_audio(post["url"])
        else:
            success, media_bytes, error = self.media.download(post["url"], post["type"])

        if not success or media_bytes is None:
            return success, media_bytes, error

        if post.get("type") != "video":
            return True, media_bytes, None

        prepared, processed_bytes, video_error, video_info = self.media.prepare_video(
            media_bytes,
            max_duration_seconds=getattr(self.config, "max_video_length_seconds", 0),
            audio_policy=getattr(self.config, "video_audio_policy", "allow_silent"),
            orientation_rule=getattr(self.config, "video_orientation_rule", "any"),
            convert_to_mp4=getattr(self.config, "video_convert_to_mp4", True),
            compression_enabled=getattr(self.config, "video_compression_enabled", True),
            compression_target_mb=getattr(self.config, "video_compression_target_mb", 40),
        )
        if prepared and processed_bytes is not None:
            post["video_info"] = dict(video_info or {})
            return True, processed_bytes, None

        return False, None, video_error or "Video did not pass configured rules"

    def _remove_preview_keyboard(self, post: Dict[str, Any]) -> None:
        """Remove inline buttons from a preview message when it is no longer actionable."""
        preview_msg_ids = post.get("preview_msg_ids")
        if isinstance(preview_msg_ids, dict):
            for raw_admin_id, raw_message_id in preview_msg_ids.items():
                try:
                    admin_id = int(raw_admin_id)
                    message_id = int(raw_message_id)
                except (TypeError, ValueError):
                    continue
                try:
                    self.telegram.edit_message_reply_markup(admin_id, message_id, None)
                except Exception:
                    pass
            return

        preview_msg_id = post.get("preview_msg_id")
        if isinstance(preview_msg_id, int):
            for admin_id in self.config.get_admin_user_ids():
                try:
                    self.telegram.edit_message_reply_markup(admin_id, preview_msg_id, None)
                except Exception:
                    pass

    def _send_admin_notice(self, text: str) -> None:
        """Send a short admin-only notice without interrupting the main flow."""
        if not self.telegram:
            return
        try:
            self._send_admin_message_html(text, disable_notification=True)
        except Exception as e:
            log(f"Could not send admin notice: {e}", "WARN")

    def _auto_recovery_enabled(self) -> bool:
        """Return True when automatic recovery actions are enabled."""
        return bool(getattr(self.config, "auto_recovery_enabled", True))

    def _parse_utc_datetime(self, value: Any) -> Optional[datetime]:
        """Parse an ISO timestamp into a UTC-aware datetime."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _record_recovery_event(
        self,
        action: str,
        detail: str,
        *,
        category: str = "runtime",
        level: str = "warn",
        post: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist an auto-recovery event and notify after repeated failures."""
        if not self._auto_recovery_enabled():
            return
        try:
            self.state.record_recovery_event(
                action,
                detail,
                category=category,
                level=level,
                post=post,
            )
            if str(level or "").lower() in {"warn", "error"}:
                self._maybe_send_recovery_notice()
        except Exception as e:
            log(f"Could not record auto-recovery event: {e}", "WARN")

    def _maybe_send_recovery_notice(self) -> None:
        """Notify the admin after repeated auto-recovery failures."""
        if not self.telegram:
            return

        window = max(1, int(getattr(self.config, "auto_recovery_notify_window_minutes", 30) or 30))
        threshold = max(1, int(getattr(self.config, "auto_recovery_notify_threshold", 3) or 3))
        count = self.state.get_recovery_failure_count(window)
        if count < threshold:
            return

        cooldown = max(
            1,
            int(getattr(self.config, "auto_recovery_notify_cooldown_minutes", 30) or 30),
        )
        last_notice = self._parse_utc_datetime(self.state.get_recovery_last_notice_utc())
        now = datetime.now(timezone.utc)
        if last_notice and (now - last_notice) < timedelta(minutes=cooldown):
            return

        recent = self.state.get_recovery_events(3, window_minutes=window)
        lines = [
            "Auto-recovery needs attention",
            "",
            f"{count} recovery failure(s) in the last {window} minute(s).",
        ]
        if recent:
            lines.append("")
            lines.append("Recent events:")
            for event in recent:
                detail = str(event.get("detail") or "").strip()
                if len(detail) > 110:
                    detail = detail[:107] + "..."
                post_id = str(event.get("post_id") or "").strip()
                post_text = f" | {post_id}" if post_id else ""
                lines.append(f"- {event.get('action', '?')}{post_text}: {detail}")

        try:
            result = self._send_admin_message_html(
                "\n".join(lines),
                disable_notification=False,
            )
            if result:
                self.state.mark_recovery_notice_sent()
                self.state.save()
        except Exception as e:
            log(f"Could not send auto-recovery notice: {e}", "WARN")

    def _compress_media_for_retry(
        self,
        post: Dict[str, Any],
        media_bytes: Any,
    ) -> tuple[bool, Any, str]:
        """Return retry-compressed media when supported by the media type."""
        if not self.media:
            return False, media_bytes, "Media handler is not available"

        media_type = str(post.get("type") or "").lower()
        if media_type == "video" and isinstance(media_bytes, (bytes, bytearray)):
            target_mb = int(getattr(self.config, "auto_recovery_video_target_mb", 30) or 30)
            success, compressed, error, info = self.media.compress_video_for_retry(
                bytes(media_bytes),
                target_mb=target_mb,
            )
            if success and compressed:
                post["video_info"] = dict(info or {})
                return True, compressed, ""
            return False, media_bytes, error or "Video retry compression failed"

        if media_type == "image" and isinstance(media_bytes, (bytes, bytearray)):
            target_mb = int(getattr(self.config, "auto_recovery_image_target_mb", 8) or 8)
            success, compressed, error = self.media.compress_image_for_retry(
                bytes(media_bytes),
                target_mb=target_mb,
            )
            if success and compressed:
                return True, compressed, ""
            return False, media_bytes, error or "Image retry compression failed"

        if media_type == "gallery" and isinstance(media_bytes, list):
            target_mb = int(getattr(self.config, "auto_recovery_image_target_mb", 8) or 8)
            changed = False
            compressed_items: List[Dict[str, Any]] = []
            errors: List[str] = []
            for index, item in enumerate(media_bytes):
                if not isinstance(item, dict):
                    compressed_items.append(item)
                    continue
                data = item.get("bytes")
                if not isinstance(data, (bytes, bytearray)) or not data:
                    compressed_items.append(dict(item))
                    continue
                success, compressed, error = self.media.compress_image_for_retry(
                    bytes(data),
                    target_mb=target_mb,
                )
                if success and compressed:
                    next_item = dict(item)
                    next_item["bytes"] = compressed
                    compressed_items.append(next_item)
                    changed = True
                else:
                    compressed_items.append(dict(item))
                    if error:
                        errors.append(f"{index + 1}: {error}")

            if changed:
                return True, compressed_items, ""
            return False, media_bytes, "; ".join(errors) or "Gallery retry compression made no changes"

        return False, media_bytes, f"Compression fallback is not supported for {media_type or 'unknown'}"

    def _send_channel_media_once(
        self,
        channel: str,
        post: Dict[str, Any],
        media_bytes: Any,
        caption: str,
        reply_markup: Optional[Dict[str, Any]],
        has_spoiler: bool,
    ) -> Dict[str, Any]:
        """Send one channel post attempt and return the Telegram message payload."""
        if post["type"] == "gallery":
            if not isinstance(media_bytes, list) or len(media_bytes) < 2:
                raise ValueError("Gallery post needs at least two media items")
            if reply_markup:
                log("Reddit link button is not attached to gallery media groups", "DEBUG")
            result = self.telegram.send_media_group(
                channel,
                media_bytes,
                caption=caption,
                has_spoiler=has_spoiler,
            )
        elif post["type"] == "image":
            result = self.telegram.send_photo(
                channel,
                media_bytes,
                caption=caption,
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            )
        else:
            result = self.telegram.send_video(
                channel,
                media_bytes,
                caption=caption,
                reply_markup=reply_markup,
                supports_streaming=True,
                has_spoiler=has_spoiler,
            )

        result_payload = result.get("result")
        if isinstance(result_payload, list):
            return result_payload[0] if result_payload else {}
        return result_payload if isinstance(result_payload, dict) else {}

    def _is_state_blocked_post(self, post: Dict[str, Any]) -> bool:
        """Return True if a candidate matches an admin block list."""
        if self.state.is_author_blocked(post.get("author", "")):
            return True
        if self.state.is_subreddit_blocked(post.get("subreddit", "")):
            return True
        if self.state.is_title_keyword_blocked(post.get("title", "")):
            return True
        return False

    def _suggest_title_keyword(self, title: str) -> str:
        """Pick a useful single-word title keyword for the block button."""
        words = []
        for raw_word in re.findall(r"[a-z0-9][a-z0-9_'-]{2,}", str(title or "").lower()):
            word = raw_word.strip("_'-")
            if word:
                words.append(word)

        best = ""
        for word in words:
            if word in self.TITLE_KEYWORD_STOPWORDS or word.isdigit():
                continue
            if len(word) > len(best):
                best = word

        if best:
            return best

        for word in words:
            if not word.isdigit():
                return word

        return ""

    def _mark_pending_skipped(self, pending: Dict[str, Any]) -> None:
        """Persist the pending post as skipped and update skip stats."""
        self.state.mark_skipped(
            pending["id"],
            pending.get("url", ""),
            pending.get("title", ""),
            pending.get("permalink", ""),
            pending.get("crosspost_parent", ""),
            post=pending,
        )
        self.state.increment_stat("total_skips")
        self.state.increment_daily_skips(self._local_date_key())

    def _mark_post_seen_after_failure(self, post: Dict[str, Any]) -> None:
        """Mark a failed post attempt as seen so it does not loop forever."""
        self.state.mark_seen(
            post["id"],
            post.get("url", ""),
            post.get("title", ""),
            post.get("permalink", ""),
            post.get("crosspost_parent", ""),
            post=post,
        )

    def _record_successful_channel_post(self, post: Dict[str, Any]) -> None:
        """Update persistent state after a channel post succeeds."""
        if post["type"] in {"image", "gallery"}:
            self.state.increment_img_streak()
        else:
            self.state.reset_img_streak()

        self.state.add_to_subreddit_streak(post["subreddit"])
        self.state.increment_stat("total_posts")
        self.state.increment_stat("total_approvals")
        self.state.increment_daily_posts(self._local_date_key())
        self.state.add_to_history(
            {
                "id": post["id"],
                "subreddit": post["subreddit"],
                "type": post["type"],
                "gallery_count": post.get("gallery_count"),
                "timestamp": now_utc().isoformat(),
            }
        )
        self.state.record_author_post(post)
        message = self._last_channel_post_message if isinstance(self._last_channel_post_message, dict) else {}
        message_id = message.get("message_id")
        self.state.record_post_analytics(
            post,
            channel=self.config.get_default_channel() or "",
            message_id=message_id if isinstance(message_id, int) else None,
            telegram_message=message,
        )
        self.state.mark_posted(
            post["id"],
            post.get("url", ""),
            post.get("title", ""),
            post.get("permalink", ""),
            post.get("crosspost_parent", ""),
            post=post,
        )

    def queue_pending(self) -> bool:
        """Move the current pending preview into the approved post queue."""
        pending = self.state.get_pending()
        if not pending:
            return False

        if not self.state.add_to_post_queue(pending):
            log(f"Pending post is already queued: {pending.get('id', 'unknown')}", "WARN")
            return False

        queue_count = self.state.get_post_queue_count()
        self._remove_preview_keyboard(pending)
        self.state.clear_pending()
        self.state.save()

        title_short = (pending.get("title") or "").strip()
        if len(title_short) > 70:
            title_short = title_short[:67] + "..."

        try:
            self._send_admin_message_html(
                f"📦 Queued #{queue_count}\n\nr/{pending.get('subreddit', '?')} - {title_short}",
                disable_notification=True,
            )
        except Exception as e:
            log(f"Could not send queue confirmation: {e}", "WARN")

        self._status_update(
            step="Queued",
            source=f"r/{pending.get('subreddit', '?')}",
            progress=str(queue_count),
            result=pending.get("type", "?"),
            event="waiting for schedule",
            level="SUCCESS",
            force=True,
        )

        return self.create_pending_approval()

    def post_next_queued_item(self) -> bool:
        """Post the next approved queue item to the channel."""
        queued = self.state.peek_next_queued_post()
        if not queued:
            return False

        queue_count = self.state.get_post_queue_count()
        log(f"Posting queued item: {queued['id']} ({queue_count} queued)")
        self._status_update(
            step="Posting queued",
            source=f"r/{queued.get('subreddit', '?')}",
            progress=queued.get("id", "queued"),
            result="posting",
            event=f"{queue_count} queued",
            level="INFO",
            force=True,
        )

        success, media_bytes, error = self._download_media_for_post(queued)
        if not success:
            log(f"Queued post download failed: {error}", "ERROR")
            self._record_emergency_failure(
                "download",
                f"queued r/{queued.get('subreddit', '?')}: {error or 'download failed'}",
            )
            self.state.pop_next_queued_post()
            self._mark_post_seen_after_failure(queued)
            self.state.set_last_tick(now_utc().isoformat())
            self.state.save()
            err_short = (error or "")[:80]
            self._status_update(
                step="Posting queued",
                source=f"r/{queued.get('subreddit', '?')}",
                progress=queued.get("id", "queued"),
                result="download failed",
                event=err_short,
                level="ERROR",
                force=True,
            )
            return False

        success = self.post_to_channel(queued, media_bytes)
        self.state.pop_next_queued_post()
        self.state.set_last_tick(now_utc().isoformat())

        if success:
            self._record_successful_channel_post(queued)
            remaining = self.state.get_post_queue_count()
            self._status_update(
                step="Posted queued",
                source=f"r/{queued.get('subreddit', '?')}",
                progress=queued.get("id", "queued"),
                result=queued.get("type", "?"),
                event=f"{remaining} queued left",
                level="SUCCESS",
                force=True,
            )
        else:
            self._mark_post_seen_after_failure(queued)
            self._status_update(
                step="Posting queued",
                source=f"r/{queued.get('subreddit', '?')}",
                progress=queued.get("id", "queued"),
                result="post failed",
                event="send failed",
                level="ERROR",
                force=True,
            )

        self.state.save()
        return success

    def pick_candidate(
        self,
        extra_seen_ids: Optional[set[str]] = None,
        extra_seen_urls: Optional[set[str]] = None,
        extra_seen_signatures: Optional[set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Pick a candidate post from Reddit.

        Returns:
            Post dictionary or None
        """
        # During Reddit cooldown, avoid fresh requests but still use cached candidates.
        reddit_backoff_active = self._is_reddit_backoff_active()
        fetch_allowed = not reddit_backoff_active

        # Determine if we should force video
        img_streak = self.state.get_img_streak()
        force_video = self.filter.should_force_video(img_streak)
        media_preference = "video" if force_video else self._recent_media_preference()

        # Get recent subreddits to avoid repetition
        recent_subs = self.state.get_subreddit_streak()

        # Collect posts from all subreddits
        all_posts = []
        # Only successfully posted or explicitly skipped items are permanently blocked.
        # Failed download/preview attempts should be retried later; otherwise a small
        # source can exhaust Reddit's visible listing.
        seen_ids = (
            self.state.get_posted_posts()
            | self.state.get_skipped_posts()
            | self.state.get_queued_post_ids()
            | (extra_seen_ids or set())
        )
        seen_urls = (
            self.state.get_posted_media_urls()
            | self.state.get_skipped_media_urls()
            | self.state.get_queued_media_urls()
            | (extra_seen_urls or set())
        )
        seen_signatures = (
            self.state.get_blocked_signatures() | self.state.get_queued_signatures() | (extra_seen_signatures or set())
        )
        collected_signatures: set[str] = set()
        duplicate_crosspost_blocking = bool(getattr(self.config, "duplicate_crosspost_blocking", True))
        title_similarity_enabled = bool(getattr(self.config, "duplicate_title_similarity_enabled", True))
        title_similarity_threshold = float(getattr(self.config, "duplicate_title_similarity_threshold", 0.88) or 0.88)
        title_similarity_limit = int(getattr(self.config, "duplicate_title_similarity_history_limit", 500) or 500)
        author_cooldown_enabled = bool(getattr(self.config, "author_cooldown_enabled", False))
        author_cooldown_hours = int(getattr(self.config, "author_cooldown_hours", 24) or 0)

        def normalize(url: str) -> str:
            return self.state._normalize_url(url)

        def post_signatures_for_compare(post: Dict[str, Any]) -> set[str]:
            if duplicate_crosspost_blocking:
                return self.state.build_post_signatures(post)

            post_copy = dict(post)
            post_copy["crosspost_parent"] = ""
            return self.state.build_post_signatures(post_copy)

        def is_duplicate_post(post: Dict[str, Any]) -> bool:
            if post.get("id") in seen_ids:
                return True

            normalized_url = normalize(post.get("url", ""))
            if normalized_url and normalized_url in seen_urls:
                return True

            if author_cooldown_enabled and author_cooldown_hours > 0:
                cooldown_match = self.state.get_author_cooldown_match(
                    post.get("author", ""),
                    author_cooldown_hours,
                )
                if cooldown_match:
                    log(
                        "Skipping author cooldown duplicate: "
                        f"u/{post.get('author', '?')} posted {cooldown_match.get('age_hours', 0):.1f}h ago",
                        "DEBUG",
                    )
                    return True

            post_signatures = post_signatures_for_compare(post)
            if post_signatures & seen_signatures:
                return True
            if post_signatures & collected_signatures:
                return True

            if title_similarity_enabled:
                title_match = self.state.find_similar_title(
                    post.get("title", ""),
                    seen_signatures | collected_signatures,
                    threshold=title_similarity_threshold,
                    limit=title_similarity_limit,
                )
                if title_match:
                    log(
                        "Skipping title-similar duplicate: "
                        f"score={title_match.get('score', 0):.2f} title={post.get('title', '')[:60]}",
                        "DEBUG",
                    )
                    return True

            return False

        def collect_unique_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            unique_posts: List[Dict[str, Any]] = []

            for post in posts:
                if self._is_state_blocked_post(post):
                    continue
                if is_duplicate_post(post):
                    continue

                collected_signatures.update(post_signatures_for_compare(post))
                unique_posts.append(post)

            return unique_posts

        total_subs = max(1, len(self.config.subreddits))
        self._status_update(
            step="Scanning sources",
            source="-",
            progress=f"0/{total_subs}",
            result="-",
            event=f"{total_subs} subs",
            level="INFO",
            force=False,
        )
        for idx, subreddit in enumerate(self.config.subreddits, 1):
            try:
                force_scan_log = idx == 1 or idx == total_subs or (idx % 4 == 0)
                if self.state.is_subreddit_blocked(subreddit):
                    self._status_update(
                        step="Scanning sources",
                        source=f"r/{subreddit}",
                        progress=f"{idx}/{total_subs}",
                        result="blocked",
                        event="Skipped source",
                        force=force_scan_log,
                    )
                    log(f"Skipping blocked subreddit r/{subreddit}", "DEBUG")
                    continue

                self._status_update(
                    step="Scanning sources",
                    source=f"r/{subreddit}",
                    progress=f"{idx}/{total_subs}",
                    event="Fetching",
                    force=force_scan_log,
                )
                # Check cache first
                cache_key = subreddit
                cached = self.state.get_cache(cache_key)

                if cached:
                    cache_time = cached.get("ts_utc")
                    cached_posts = cached.get("raw_posts") or cached.get("posts", [])

                    try:
                        cache_dt = datetime.fromisoformat(cache_time)
                        age_minutes = (now_utc() - cache_dt).total_seconds() / 60
                        max_cache_minutes = self.config.reddit_cache_minutes
                        if reddit_backoff_active:
                            # Reddit is rate-limiting us; stale cache is better than no preview.
                            max_cache_minutes = max(max_cache_minutes, 12 * 60)

                        if age_minutes < max_cache_minutes:
                            # Re-filter cached posts against current rules and seen sets.
                            posts = self.filter.filter_posts(cached_posts, verbose=False)
                            posts = collect_unique_posts(posts)
                            video_count = sum(1 for p in posts if p.get("type") == "video")
                            self._status_update(
                                result=f"cached {len(posts)}/{len(cached_posts)} | v{video_count} | age {age_minutes:.1f}m",
                                event="Cache hit",
                                force=force_scan_log,
                            )
                            log(
                                f"Using cached r/{subreddit} ({len(posts)}/{len(cached_posts)} usable, age={age_minutes:.1f}m)",
                                "DEBUG",
                            )
                            if posts:
                                all_posts.extend(posts)
                                if force_video and video_count == 0:
                                    log(
                                        f"Cached r/{subreddit} window has no videos; using image fallback until cache refresh",
                                        "INFO",
                                    )
                                continue
                    except Exception as e:
                        log(f"Cache parse error for r/{subreddit}: {e}", "WARN")

                if not fetch_allowed:
                    self._status_update(
                        result="cache only",
                        event="Reddit cooldown",
                        force=force_scan_log,
                    )
                    continue

                # Fetch from Reddit
                try:
                    rule = self.config.get_effective_subreddit_rule(subreddit)
                    media_type_rule = str(rule.get("media_type", "any") or "any")
                    source_preferred_type = (
                        "video" if media_preference == "video" and media_type_rule not in {"image", "gallery"} else None
                    )
                    fetched_posts, fetched_count, used_age_fallback, fetch_limit_used = (
                        self._fetch_subreddit_candidates(
                            subreddit,
                            seen_ids,
                            seen_urls,
                            seen_signatures,
                            preferred_type=source_preferred_type,
                            block_crossposts=duplicate_crosspost_blocking,
                        )
                    )
                    posts = collect_unique_posts(fetched_posts)

                    accepted_count = len(posts)
                    video_count = sum(1 for p in posts if p.get("type") == "video")
                    self._status_update(
                        result=(
                            f"fetched {fetched_count}, ok {accepted_count}, v{video_count}"
                            + (f" | top {fetch_limit_used}" if fetch_limit_used > 30 else "")
                            + (" | old" if used_age_fallback else "")
                        ),
                        event="Fetched",
                        force=force_scan_log,
                    )

                    log(f"After filtering: {len(posts)} posts from r/{subreddit}", "DEBUG")

                    # Cache fetched candidates before the second-pass uniqueness filter so
                    # later preview attempts do not starve themselves on a shrinking cache.
                    self.state.set_cache(
                        cache_key,
                        {
                            "ts_utc": now_utc().isoformat(),
                            "raw_posts": fetched_posts,
                            "posts": fetched_posts,
                        },
                    )

                    all_posts.extend(posts)

                except Exception as e:
                    error_str = str(e)
                    response = getattr(e, "response", None)
                    status_code = getattr(response, "status_code", None)
                    if isinstance(e, RedditConfigurationError):
                        if self.reddit and self.reddit.uses_oauth():
                            log(f"Reddit configuration issue: {e}", "ERROR")
                            self._pause_for_reddit_configuration_issue(error_str)
                        else:
                            log(f"Reddit legacy access blocked: {e}", "WARN")
                            self._set_reddit_backoff(
                                reason="Reddit blocked anonymous API access",
                                min_seconds=25 * 60,
                                max_seconds=45 * 60,
                                error=e,
                                emergency_category="reddit",
                            )
                        return None
                    if status_code == 429 or "429" in error_str:
                        if all_posts:
                            log(
                                "Reddit returned 429 after candidates were collected; selecting from cached/fetched candidates",
                                "WARN",
                            )
                            self._set_reddit_backoff(
                                reason="Reddit returned 429 Too Many Requests",
                                min_seconds=20 * 60,
                                max_seconds=35 * 60,
                                error=e,
                                emergency_category="reddit",
                            )
                            break
                        else:
                            self._set_reddit_backoff(
                                reason="Reddit returned 429 Too Many Requests",
                                min_seconds=20 * 60,
                                max_seconds=35 * 60,
                                error=e,
                                emergency_category="reddit",
                            )
                        return None
                    if status_code == 403 or "403" in error_str:
                        self._set_reddit_backoff(
                            reason="Reddit returned 403 Forbidden",
                            min_seconds=25 * 60,
                            max_seconds=45 * 60,
                            error=e,
                            emergency_category="reddit",
                        )
                        return None
                    else:
                        log(f"Error fetching r/{subreddit}: {e}", "ERROR")
                        self._record_emergency_failure(
                            "reddit",
                            f"r/{subreddit}: {e}",
                        )
                        if self.state.is_paused():
                            return None
                        import traceback

                        traceback.print_exc()

            except Exception as e:
                log(f"Unexpected error processing r/{subreddit}: {e}", "ERROR")
                self._record_emergency_failure(
                    "reddit",
                    f"r/{subreddit}: {e}",
                )
                if self.state.is_paused():
                    return None
                import traceback

                traceback.print_exc()

        log(f"Total posts collected: {len(all_posts)}", "DEBUG")
        self._status_update(
            step="Scanning done",
            source="-",
            progress=f"{total_subs} subs",
            result=f"candidates {len(all_posts)}",
            event="selection",
            level="INFO",
            force=False,
        )

        if not all_posts:
            log("No candidate posts found", "WARN")
            self._record_emergency_failure("empty_feed", "no candidate posts found")
            return None

        # Pick best post. Keep the channel close to a 50/50 image/video mix by
        # preferring the media group that is behind in recent successful posts.
        force_type = "video" if media_preference == "video" else None
        selection_posts = all_posts
        if media_preference == "image":
            image_posts = [p for p in all_posts if p.get("type") in {"image", "gallery"}]
            if image_posts:
                selection_posts = image_posts
        avoid_sub = recent_subs[-1] if recent_subs else None

        post = self.filter.pick_best_post(
            selection_posts,
            force_type,
            avoid_sub,
            recent_subreddits=recent_subs,
            img_streak=img_streak,
        )

        if post:
            score_text = ""
            if "_score" in post:
                score_text = f" score={post['_score']}"
            log(
                f"Selected {post['type']} from r/{post['subreddit']}{score_text}: {post['title'][:50]}",
                "DEBUG",
            )
            if force_video and post.get("type") != "video":
                self.state.reset_img_streak()
                self.state.save()
                log(
                    "No video candidates available; temporarily disabling forced-video mode",
                    "INFO",
                )
            self._status_update(
                step="Candidate picked",
                source=f"r/{post['subreddit']}",
                progress=self._status.get("progress", "-"),
                result=post.get("type", "?"),
                event=f"score {post['_score']}" if "_score" in post else "selected",
                force=True,
            )
        else:
            log("No post selected after filtering", "WARN")
            self._record_emergency_failure("empty_feed", "no post selected after filtering")

        return post

    def create_pending_approval(
        self,
        *,
        use_queue: bool = False,
        exclude_ids: Optional[set[str]] = None,
        exclude_urls: Optional[set[str]] = None,
        exclude_signatures: Optional[set[str]] = None,
    ) -> bool:
        """
        Create a pending approval item.

        Returns:
            True if successful
        """
        if use_queue and self.state.has_queued_posts():
            return self.post_next_queued_item()

        # Check rate limit
        preview_times = self.state.get_preview_times()
        if self.scheduler.should_rate_limit_previews(preview_times, self.config.max_previews_per_10min, 10):
            log("Preview rate limit exceeded, waiting", "WARN")
            return False

        # Clean old preview times
        cleaned = self.scheduler.clean_old_preview_times(preview_times, 10)
        self.state.set_preview_times(cleaned)

        # Try multiple candidates in one go so a bad/too-small item doesn't stall the bot.
        max_attempts = 8
        attempted_ids: set[str] = set(exclude_ids or set())
        attempted_urls: set[str] = set(exclude_urls or set())
        attempted_signatures: set[str] = set(exclude_signatures or set())
        for attempt in range(1, max_attempts + 1):
            post = self.pick_candidate(
                extra_seen_ids=attempted_ids,
                extra_seen_urls=attempted_urls,
                extra_seen_signatures=attempted_signatures,
            )
            if not post:
                return False

            log(
                f"Candidate attempt {attempt}/{max_attempts}: {post['type']} from r/{post['subreddit']}",
                "DEBUG",
            )
            title_short = (post.get("title") or "").strip()
            if len(title_short) > 60:
                title_short = title_short[:57] + "..."
            self._status_update(
                step="Candidate",
                source=f"r/{post['subreddit']}",
                progress=f"{attempt}/{max_attempts}",
                result=post.get("type", "?"),
                event=title_short or "picked",
                force=True,
            )

            # Track the attempt so this approval cycle moves on if the candidate fails.
            attempted_ids.add(str(post.get("id", "")).strip())
            normalized_url = self.state._normalize_url(post.get("url", "") or "")
            if normalized_url:
                attempted_urls.add(normalized_url)
            attempted_signatures.update(self.state.build_post_signatures(post))

            # Persist attempted history for diagnostics without permanently blocking retries.
            self.state.mark_seen(
                post["id"],
                post.get("url", ""),
                post.get("title", ""),
                post.get("permalink", ""),
                post.get("crosspost_parent", ""),
                post=post,
            )
            self.state.save()

            # Download media
            log(f"Downloading media from: {post['url']}", "DEBUG")
            self._status_update(
                step="Downloading media",
                source=f"r/{post['subreddit']}",
                progress=f"{attempt}/{max_attempts}",
                event="Downloading",
                force=True,
            )

            success, media_bytes, error = self._download_media_for_post(post)

            if not success:
                log(f"Download failed: {error}", "ERROR")
                self._record_emergency_failure(
                    "download",
                    f"preview r/{post.get('subreddit', '?')}: {error or 'download failed'}",
                )
                err_short = (error or "")[:80]
                self._status_update(
                    step="Downloading media",
                    source=f"r/{post['subreddit']}",
                    progress=f"{attempt}/{max_attempts}",
                    result="download failed",
                    event=err_short,
                    level="ERROR",
                    force=True,
                )
                if self.state.is_paused():
                    return False
                continue

            # Validate image quality if image or gallery
            if post["type"] == "gallery":
                if not isinstance(media_bytes, list):
                    media_bytes = []
                acceptable, accepted_items, reason = self._validate_gallery_media_items(media_bytes)
                if not acceptable:
                    log(f"Gallery quality check failed: {reason} (skipping)", "WARN")
                    reason_short = (reason or "")[:80]
                    self._status_update(
                        step="Validating media",
                        source=f"r/{post['subreddit']}",
                        progress=f"{attempt}/{max_attempts}",
                        result="rejected",
                        event=reason_short,
                        force=True,
                    )
                    continue
                media_bytes = accepted_items
                post["gallery_count"] = len(accepted_items)
            elif post["type"] == "image":
                acceptable, reason = self.media.is_image_quality_acceptable(
                    media_bytes,
                    self.config.min_image_width,
                    **self._image_quality_options(),
                )
                if not acceptable:
                    log(f"Image quality check failed: {reason} (skipping)", "WARN")
                    reason_short = (reason or "")[:80]
                    self._status_update(
                        step="Validating media",
                        source=f"r/{post['subreddit']}",
                        progress=f"{attempt}/{max_attempts}",
                        result="rejected",
                        event=reason_short,
                        force=True,
                    )
                    continue

            # Send preview
            msg_id = self.send_preview_to_admin(post, media_bytes)
            if not msg_id:
                log("Failed to send preview to admin (skipping candidate)", "ERROR")
                self._status_update(
                    step="Approval",
                    source=f"r/{post['subreddit']}",
                    progress=f"{attempt}/{max_attempts}",
                    result="preview failed",
                    event="admin send failed",
                    level="ERROR",
                    force=True,
                )
                if self.state.is_paused():
                    return False
                continue

            # Create pending item
            deadline = self.scheduler.get_approval_deadline()

            pending = {
                "id": post["id"],
                "subreddit": post["subreddit"],
                "author": post.get("author", ""),
                "title": post["title"],
                "type": post["type"],
                "url": post["url"],
                "caption": self.make_caption(post),
                "preview_msg_id": msg_id,
                "preview_msg_ids": dict(post.get("preview_msg_ids") or {}),
                "deadline_utc": deadline.isoformat(),
                "pending_created_utc": now_utc().isoformat(),
                "permalink": post.get("permalink", ""),
                "crosspost_parent": post.get("crosspost_parent", ""),
                "created_utc": post.get("created_utc"),
                "upvotes": post.get("upvotes", 0),
                "num_comments": post.get("num_comments", 0),
                "score": post.get("_score"),
            }
            if post.get("type") == "gallery":
                pending["gallery_items"] = list(post.get("gallery_items", []) or [])
                pending["gallery_count"] = post.get(
                    "gallery_count",
                    len(pending["gallery_items"]),
                )
            elif post.get("type") == "video" and isinstance(post.get("video_info"), dict):
                pending["video_info"] = dict(post["video_info"])

            self.state.set_pending(pending)
            self.state.add_preview_time(now_utc().isoformat())
            self.state.set_last_tick(now_utc().isoformat())
            self.state.save()

            log(f"Pending approval created, auto-post at {deadline.isoformat()}", "SUCCESS")
            auto_mins = max(0, int((deadline - now_utc()).total_seconds() / 60))
            self._status_update(
                step="Approval",
                source=f"r/{post['subreddit']}",
                progress=pending["id"],
                result=f"auto {auto_mins}m",
                event="waiting admin",
                level="SUCCESS",
                force=True,
            )
            return True

        log("Reached max candidate attempts without a valid preview", "WARN")
        self._set_reddit_backoff(
            reason="No valid preview candidates found",
            min_seconds=8 * 60,
            max_seconds=12 * 60,
            emergency_category="empty_feed",
        )
        return False

    def approve_pending(self) -> bool:
        """
        Approve and post the pending item.

        Returns:
            True if successful
        """
        pending = self.state.get_pending()
        if not pending:
            return False

        log(f"Approving pending: {pending['id']}")
        self._status_update(
            step="Posting",
            source=f"r/{pending.get('subreddit', '?')}",
            progress=pending.get("id", "pending"),
            result="posting",
            event="downloading",
            level="INFO",
            force=True,
        )

        # Download media again (could be expired from cache)
        success, media_bytes, error = self._download_media_for_post(pending)

        if not success:
            log(f"Download failed during approval: {error}", "ERROR")
            self._record_emergency_failure(
                "download",
                f"approval r/{pending.get('subreddit', '?')}: {error or 'download failed'}",
            )
            self.state.mark_seen(
                pending["id"],
                pending.get("url", ""),
                pending.get("title", ""),
                pending.get("permalink", ""),
                pending.get("crosspost_parent", ""),
                post=pending,
            )
            self.state.clear_pending()
            self.state.save()
            err_short = (error or "")[:80]
            self._status_update(
                step="Posting",
                source=f"r/{pending.get('subreddit', '?')}",
                progress=pending.get("id", "pending"),
                result="download failed",
                event=err_short,
                level="ERROR",
                force=True,
            )
            return False

        # Post to channel
        success = self.post_to_channel(pending, media_bytes)

        if success:
            self._record_successful_channel_post(pending)
            self._status_update(
                step="Posted",
                source=f"r/{pending.get('subreddit', '?')}",
                progress=pending.get("id", "pending"),
                result=pending.get("type", "?"),
                event=f"posted to {self.config.get_default_channel() or '?'}",
                level="SUCCESS",
                force=True,
            )
        else:
            # Even on failure, mark as seen to avoid looping
            self.state.mark_seen(
                pending["id"],
                pending.get("url", ""),
                pending.get("title", ""),
                pending.get("permalink", ""),
                pending.get("crosspost_parent", ""),
                post=pending,
            )
            self._status_update(
                step="Posting",
                source=f"r/{pending.get('subreddit', '?')}",
                progress=pending.get("id", "pending"),
                result="post failed",
                event="send failed",
                level="ERROR",
                force=True,
            )

        # Clear pending

        self._remove_preview_keyboard(pending)

        self.state.clear_pending()
        self.state.save()

        return success

    def skip_pending(self) -> bool:
        """
        Skip the pending item.

        Returns:
            True if successful
        """
        pending = self.state.get_pending()
        if not pending:
            return False

        log(f"Skipping pending: {pending['id']}")

        # Mark as skipped (also marks seen + seen media)
        self._mark_pending_skipped(pending)
        self._status_update(
            step="Skipped",
            source=f"r/{pending.get('subreddit', '?')}",
            progress=pending.get("id", "pending"),
            result=pending.get("type", "?"),
            event="skipped by admin",
            level="INFO",
            force=True,
        )

        self._remove_preview_keyboard(pending)

        self.state.clear_pending()
        self.state.save()

        cooldown = random.uniform(1.0, 2.5)
        log(f"Skip cooldown: {cooldown:.1f}s", "DEBUG")
        self._send_admin_notice("⏭️ Preview skipped. Finding the next one...")
        self._schedule_pending_refresh(
            label="skip refresh preview",
            delay_seconds=cooldown,
            no_result_notice="No next preview found right now.",
        )
        return True

    def reroll_pending(self) -> bool:
        """
        Replace the current preview without permanently blocking the post.

        The current post is excluded from the next candidate search so the admin
        does not immediately see the same preview again.
        """
        pending = self.state.get_pending()
        if not pending:
            return False

        log(f"Re-rolling pending: {pending['id']}")
        exclude_ids = {str(pending.get("id", "")).strip()}
        exclude_urls = set()
        normalized_url = self.state._normalize_url(pending.get("url", "") or "")
        if normalized_url:
            exclude_urls.add(normalized_url)
        exclude_signatures = self.state.build_post_signatures(post=pending)

        self._status_update(
            step="Re-rolling",
            source=f"r/{pending.get('subreddit', '?')}",
            progress=pending.get("id", "pending"),
            result=pending.get("type", "?"),
            event="admin requested another candidate",
            level="INFO",
            force=True,
        )

        self._remove_preview_keyboard(pending)
        self.state.clear_pending()
        self.state.save()
        self._send_admin_notice("Re-rolling preview...")

        self._schedule_pending_refresh(
            label="reroll refresh preview",
            delay_seconds=0.25,
            no_result_notice="No alternate preview found right now.",
            exclude_ids=exclude_ids,
            exclude_urls=exclude_urls,
            exclude_signatures=exclude_signatures,
        )
        return True

    def block_pending_author(self) -> bool:
        """Block the pending post author, skip the current post, and find another."""
        pending = self.state.get_pending()
        if not pending:
            return False

        author = self.state.normalize_author(pending.get("author", ""))
        if not author:
            self._send_admin_notice("This preview has no author metadata to block.")
            return False

        added = self.state.block_author(author)
        self._mark_pending_skipped(pending)
        self._remove_preview_keyboard(pending)
        self.state.clear_pending()
        self.state.save()

        action = "Blocked" if added else "Already blocked"
        self._send_admin_notice(f"{action} author u/{author}. Looking for the next preview.")
        self._status_update(
            step="Blocked author",
            source=f"r/{pending.get('subreddit', '?')}",
            progress=pending.get("id", "pending"),
            result=f"u/{author}",
            event="admin block",
            level="INFO",
            force=True,
        )

        self._schedule_pending_refresh(
            label="author block refresh preview",
            delay_seconds=0.25,
            no_result_notice="No next preview found after applying the author block.",
        )
        return True

    def block_pending_subreddit(self) -> bool:
        """Block the pending post subreddit, skip the current post, and find another."""
        pending = self.state.get_pending()
        if not pending:
            return False

        subreddit = self.state.normalize_subreddit(pending.get("subreddit", ""))
        if not subreddit:
            self._send_admin_notice("This preview has no subreddit metadata to block.")
            return False

        added = self.state.block_subreddit(subreddit)
        self._mark_pending_skipped(pending)
        self._remove_preview_keyboard(pending)
        self.state.clear_pending()
        self.state.save()

        action = "Blocked" if added else "Already blocked"
        self._send_admin_notice(f"{action} subreddit r/{subreddit}. Looking for the next preview.")
        self._status_update(
            step="Blocked subreddit",
            source=f"r/{subreddit}",
            progress=pending.get("id", "pending"),
            result="source blocked",
            event="admin block",
            level="INFO",
            force=True,
        )

        self._schedule_pending_refresh(
            label="subreddit block refresh preview",
            delay_seconds=0.25,
            no_result_notice="No next preview found after applying the subreddit block.",
        )
        return True

    def block_pending_title_keyword(self) -> bool:
        """Block a keyword inferred from the pending title and find another preview."""
        pending = self.state.get_pending()
        if not pending:
            return False

        keyword = self._suggest_title_keyword(pending.get("title", ""))
        if not keyword:
            self._send_admin_notice("Could not infer a useful title keyword to block.")
            return False

        added = self.state.block_title_keyword(keyword)
        self._mark_pending_skipped(pending)
        self._remove_preview_keyboard(pending)
        self.state.clear_pending()
        self.state.save()

        action = "Blocked" if added else "Already blocked"
        self._send_admin_notice(f"{action} title keyword '{keyword}'. Looking for the next preview.")
        self._status_update(
            step="Blocked title",
            source=f"r/{pending.get('subreddit', '?')}",
            progress=pending.get("id", "pending"),
            result=keyword,
            event="admin block",
            level="INFO",
            force=True,
        )

        self._schedule_pending_refresh(
            label="title block refresh preview",
            delay_seconds=0.25,
            no_result_notice="No next preview found after applying the title block.",
        )
        return True

    def process_telegram_updates(self, timeout: int = 20) -> None:
        """Process Telegram updates (button clicks, commands)"""
        if not self.telegram:
            return
        if not self._telegram_update_lock.acquire(blocking=False):
            return

        try:
            offset = self.state.get_update_offset()
            updates = self.telegram.get_updates(offset, timeout=timeout)

            if not updates:
                return

            max_update_id = None

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

                # Handle callback queries (buttons)
                callback = update.get("callback_query")
                if callback:
                    self.handle_callback_query(callback)
                    continue

                channel_post = update.get("channel_post") or update.get("edited_channel_post")
                if channel_post:
                    self.handle_channel_post_update(channel_post)
                    continue

                # Handle text messages (commands)
                message = update.get("message")
                if message:
                    self.handle_text_message(message)

            # Update offset. During post search/upload we defer the disk write so
            # Telegram menus stay responsive without racing the long posting job's saves.
            if max_update_id is not None:
                self.state.set_update_offset(max_update_id + 1)
                if self._posting_work_lock.locked():
                    self._update_offset_dirty = True
                else:
                    self.state.save()
                    self._update_offset_dirty = False
        finally:
            self._telegram_update_lock.release()

    def handle_channel_post_update(self, message: Dict[str, Any]) -> None:
        """Capture Telegram channel post metrics when update payloads include them."""
        if self.state.update_post_analytics_from_telegram_message(message):
            message_id = message.get("message_id", "?")
            log(f"Updated performance analytics for channel post {message_id}", "DEBUG")

    def handle_callback_query(self, callback: Dict[str, Any]) -> None:
        """Handle callback query from button press"""
        callback_id = callback.get("id")
        data = callback.get("data")
        from_user = callback.get("from", {})
        from_id = from_user.get("id")

        # Answer callback
        if callback_id:
            self.telegram.answer_callback_query(callback_id)

        # Security check: only configured admin users can press buttons
        if not self.config.is_admin_user(from_id):
            log(f"Ignoring callback from non-admin: {from_id}", "WARN")
            return

        if isinstance(data, str) and data.startswith("menu:"):
            self.handle_admin_menu_callback(callback)
            return

        if not self.state.has_pending():
            log("Callback received but no pending item")
            return

        pending_actions = {
            "approve": "Admin approved via button",
            "skip": "Admin skipped via button",
        }

        if data in pending_actions:
            log(pending_actions[data])
            pending_snapshot = copy.deepcopy(self.state.get_pending())
            actor_label = self._format_admin_actor(from_user)
            result = self._run_posting_work(
                f"pending callback {data}",
                lambda: self._execute_pending_action(str(data)),
                notify_busy=True,
            )
            if result and pending_snapshot:
                self._notify_admin_pending_action(str(data), actor_label, pending_snapshot)
            if result is None and callback_id:
                self.telegram.answer_callback_query(
                    callback_id,
                    text="Busy searching or posting. Try again shortly.",
                )
            return

    def _handle_command_message(self, message: Dict[str, Any]) -> None:
        """Handle slash commands sent in Telegram chats."""
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        text = (message.get("text", "") or "").strip()
        if not text.startswith("/"):
            return

        if chat.get("type") != "private":
            return

        is_admin = self.config.is_admin_user(chat_id)
        command = text.split(maxsplit=1)[0].lower()

        if not is_admin and command not in {"/start", "/help"}:
            return

        # Process command
        if self.traffic:
            try:
                self.traffic.track_command(message, command, is_admin=is_admin)
            except Exception as e:
                log(f"Could not track bot traffic: {e}", "WARN")

        if is_admin and command in {"/menu", "/start"}:
            try:
                self.send_admin_menu(chat_id)
            except Exception as e:
                log(f"Failed to send admin menu: {e}", "ERROR")
            return

        response = self.commands.process_command(text, is_admin=is_admin)

        if response:
            try:
                reply_markup = None
                parse_mode = None
                message_text = response

                if not is_admin and command in {"/start", "/help"}:
                    parse_mode = "HTML"
                elif is_admin:
                    parse_mode = "HTML"
                    message_text = self._format_admin_message_html(response)

                self.telegram.send_message(
                    chat_id,
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except Exception as e:
                log(f"Failed to send command response: {e}", "ERROR")

    def _handle_admin_menu_pending_text(self, message: Dict[str, Any]) -> bool:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        from_user = message.get("from", {}) or {}
        user_id = from_user.get("id")
        text = (message.get("text", "") or "").strip()
        if not self.config.is_admin_user(chat_id) or not self.config.is_admin_user(user_id):
            return False

        pending = self._admin_menu_pending_inputs.get(int(user_id))
        if pending is None:
            return False

        if text.casefold() in {"/menu", "/start"}:
            self._admin_menu_pending_inputs.pop(int(user_id), None)
            return False

        if text.casefold() in {"cancel", "/cancel"}:
            self._admin_menu_pending_inputs.pop(int(user_id), None)
            self._send_admin_menu_page(chat_id, pending.page, "🚫 Edit cancelled.")
            return True

        try:
            value = self._parse_admin_menu_input(text, pending.parser)
            if pending.rule_key:
                notice = self._save_weekly_rule_value(pending.rule_key, pending.field, value)
                page = f"weekly_{pending.rule_key}"
            else:
                notice = self._save_admin_menu_value(pending.field, value)
                page = pending.page
        except Exception as e:
            self.telegram.send_message(
                chat_id,
                self._format_admin_message_html(f"⚠️ Could not save that value.\n\n{e}\n\n{pending.prompt}"),
                parse_mode="HTML",
            )
            return True

        self._admin_menu_pending_inputs.pop(int(user_id), None)
        self._send_admin_menu_page(chat_id, page, notice)
        return True

    def handle_text_message(self, message: Dict[str, Any]) -> None:
        """Handle text message (commands and admin menu edits)."""
        text = (message.get("text", "") or "").strip()

        if not text:
            return

        if self._handle_admin_menu_pending_text(message):
            return

        self._handle_command_message(message)

    def check_auto_approve(self) -> None:
        """Check if pending item should auto-approve"""
        if not self.state.has_pending():
            return

        pending = self.state.get_pending()
        deadline = pending.get("deadline_utc")

        if self.scheduler.has_approval_expired(deadline):
            log("Auto-approval deadline reached")
            self._run_posting_work("auto approval", self.approve_pending)

    def recover_stuck_pending(self) -> bool:
        """Skip an expired pending item when it has been stuck too long."""
        if not self._auto_recovery_enabled() or not self.state.has_pending():
            return False

        pending = self.state.get_pending()
        if not isinstance(pending, dict):
            return False

        now = datetime.now(timezone.utc)
        changed = False
        created_at = self._parse_utc_datetime(pending.get("pending_created_utc"))
        if created_at is None:
            created_at = now
            pending["pending_created_utc"] = now.isoformat()
            changed = True

        deadline = self._parse_utc_datetime(pending.get("deadline_utc"))
        if deadline and now < deadline:
            if changed:
                self.state.set_pending(pending)
                self.state.save()
            return False

        stuck_minutes = max(
            1,
            int(getattr(self.config, "auto_recovery_stuck_pending_minutes", 90) or 90),
        )
        reference = deadline or created_at
        overdue_seconds = (now - reference).total_seconds()
        if overdue_seconds < stuck_minutes * 60:
            if changed:
                self.state.set_pending(pending)
                self.state.save()
            return False

        post_id = str(pending.get("id") or "pending")
        subreddit = str(pending.get("subreddit") or "?")
        minutes_text = max(1, int(overdue_seconds // 60))
        reason = (
            f"Skipped stuck pending item {post_id} from r/{subreddit} "
            f"after {minutes_text} minute(s) past recovery reference"
        )
        log(reason, "WARN")
        self._record_recovery_event(
            "stuck_pending_skipped",
            reason,
            category="pending",
            level="error",
            post=pending,
        )
        self._mark_pending_skipped(pending)
        self._remove_preview_keyboard(pending)
        self.state.clear_pending()
        self.state.set_last_tick(now.isoformat())
        self.state.save()
        self._send_admin_notice(
            "Auto-recovery skipped a stuck pending item.\n\n"
            f"Post: r/{subreddit} / {post_id}\n"
            f"Reason: pending for {minutes_text} minute(s) past recovery reference."
        )
        return True

    def should_create_next_post(self) -> bool:
        """Check if we should create next post"""
        # Don't create if already pending
        if self.state.has_pending():
            return False

        # Don't create if paused
        if self.state.is_paused():
            return False

        has_queue = self.state.has_queued_posts()

        # Don't fetch fresh Reddit candidates while Reddit is rate-limiting or cooling down.
        if not has_queue and self._is_reddit_backoff_active(log_remaining=False):
            return False

        # Check active hours
        if not self.scheduler.is_within_active_hours():
            return False

        # Check daily limit
        if self.scheduler.has_reached_daily_limit(self.state.get_daily_posts_count(self._local_date_key())):
            log("Daily post limit reached")
            return False

        # Check time interval
        last_tick = self.state.get_last_tick()
        return self.scheduler.should_create_next_post(last_tick)

    def run(self) -> None:
        """Main bot loop"""
        if not self.initialize():
            log("Failed to initialize bot", "ERROR")
            return

        if not self.bootstrap():
            log("Failed to bootstrap bot", "ERROR")
            return
        self.running = True

        # Concise startup status line
        channel = self.config.get_default_channel() or "?"
        subs = len(self.config.subreddits)
        interval_min = self.config.post_interval_minutes
        self._status_update(
            step="Running",
            source="-",
            progress=f"{subs} subs",
            result=f"interval {interval_min}m",
            event=f"channel {channel}",
            level="SUCCESS",
            force=True,
        )
        self._start_telegram_update_worker()

        # Create first pending if none exists and the approved queue is empty.
        if not self.state.has_pending() and not self.state.has_queued_posts():
            log("No pending item, creating first preview...", "DEBUG")
            self._run_posting_work("first preview creation", self.create_pending_approval)
        elif self.state.has_queued_posts():
            log(f"{self.state.get_post_queue_count()} approved queued post(s) ready for schedule", "INFO")

        try:
            while self.running:
                try:
                    self._persist_deferred_update_offset()

                    # Recover or auto-approve stale pending previews.
                    if not self._posting_work_lock.locked() and not self.recover_stuck_pending():
                        self.check_auto_approve()

                    # Create next post if needed
                    if not self._posting_work_lock.locked() and self.should_create_next_post():
                        log("Time to create next post", "DEBUG")
                        self._run_posting_work(
                            "scheduled preview creation",
                            lambda: self.create_pending_approval(use_queue=True),
                        )

                    # Periodic live status line (rate-limited)
                    self._log_status()

                except Exception as e:
                    log(f"Error in main loop: {e}", "ERROR")
                    try:
                        self.state.record_failure(str(e), self._local_date_key())
                        self.state.save()
                    except Exception:
                        pass

                # Sleep
                time.sleep(2)

        except KeyboardInterrupt:
            log("\nStopping bot...")
            self.stop()

    def stop(self) -> None:
        """Stop the bot gracefully"""
        self.running = False
        self._telegram_update_stop.set()
        thread = self._telegram_update_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._persist_deferred_update_offset()
        self.state.save()
        log("Bot stopped, state saved", "SUCCESS")
