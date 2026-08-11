"""
Bot command handler.
Processes commands sent to the bot via Telegram.
"""

import subprocess
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone
from utils import log, format_seconds, get_log_history


class CommandHandler:
    """Handles admin commands and public help/start replies."""

    def __init__(
        self,
        config,
        state_manager,
        scheduler,
        traffic_service=None,
        telegram_handler=None,
        reddit_handler=None,
        media_handler=None,
    ):
        """
        Initialize command handler.

        Args:
            config: Config object
            state_manager: StateManager instance
            scheduler: Scheduler instance
        """
        self.config = config
        self.state = state_manager
        self.scheduler = scheduler
        self.traffic = traffic_service
        self.telegram = telegram_handler
        self.reddit = reddit_handler
        self.media = media_handler

        # Command registry
        self.commands = {
            "/start": self.cmd_start,
            "/menu": self.cmd_menu,
            "/help": self.cmd_help,
            "/skip": self.cmd_skip,
        }

    def process_command(self, text: str, is_admin: bool = False) -> Optional[str]:
        """
        Process command text and return response.

        Args:
            text: Command text from user

        Returns:
            Response text or None
        """
        text = text.strip()

        if not text.startswith("/"):
            return None

        # Split command and args
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        handler = self.commands.get(command)
        if not is_admin and command not in {"/start", "/help"}:
            return None

        if handler:
            try:
                if command in {"/start", "/help", "/menu"}:
                    return handler(args, is_admin=is_admin)
                return handler(args)
            except Exception as e:
                log(f"Error processing command {command}: {e}", "ERROR")
                return f"❌ Error processing command: {str(e)}"

        return None

    def _local_datetime(self) -> datetime:
        """Return the schedule-local datetime used by daily reports."""
        try:
            return self.scheduler._local_now()
        except Exception:
            return datetime.now(timezone.utc).astimezone()

    def _local_date_key(self) -> str:
        """Return the local date key used by daily counters."""
        return self._local_datetime().date().isoformat()

    def _format_next_schedule_line(self, posts_today: int) -> str:
        """Return a compact next-schedule summary."""
        limit = self.scheduler.get_effective_daily_limit()
        if limit > 0 and posts_today >= limit:
            return f"Daily limit reached ({posts_today}/{limit})"

        if self.state.is_paused():
            return "Paused by admin"

        try:
            current_schedule = self.scheduler.get_effective_schedule()
            if current_schedule.get("paused"):
                return f"Paused by weekly rule ({current_schedule.get('schedule_key', 'global')})"
        except Exception:
            current_schedule = {}

        try:
            if not self.scheduler.is_within_active_hours():
                next_active = self.scheduler.get_next_active_time()
                if next_active:
                    local_next = next_active.astimezone(self._local_datetime().tzinfo)
                    return f"Inactive until {local_next:%a %H:%M}"
                return "Inactive"
        except Exception:
            pass

        try:
            last_tick = self.state.get_last_tick()
            next_time = self.scheduler.calculate_next_post_time(last_tick)
            now = datetime.now(timezone.utc)
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=timezone.utc)
            if next_time <= now:
                return "Ready now"
            wait_seconds = int((next_time - now).total_seconds())
            local_next = next_time.astimezone(self._local_datetime().tzinfo)
            return f"In {format_seconds(wait_seconds)} ({local_next:%H:%M})"
        except Exception:
            return "Unknown"

    def _format_health_status(self, ok: bool, detail: str) -> str:
        """Format one health status line without exposing secrets."""
        return f"{'OK' if ok else 'WARN'} - {detail}"

    def _format_utc_timestamp(self, value: Any) -> str:
        """Format an ISO timestamp as compact UTC text."""
        if not value:
            return "unknown time"
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return str(value).replace("T", " ")[:19]

    def _trim_health_text(self, value: Any, limit: int = 120) -> str:
        """Return a one-line health detail."""
        text = str(value or "").strip().replace("\n", " ")
        return text[: limit - 3] + "..." if len(text) > limit else text

    def _build_reddit_handler(self):
        """Return a Reddit handler for health checks."""
        if self.reddit is not None:
            return self.reddit
        from reddit_handler import RedditHandler

        return RedditHandler(
            self.config.user_agent,
            reddit_client_id=getattr(self.config, "reddit_client_id", ""),
            reddit_client_secret=getattr(self.config, "reddit_client_secret", ""),
            domain_downloaders_enabled=getattr(self.config, "domain_downloaders_enabled", True),
            imgur_album_downloads_enabled=getattr(self.config, "imgur_album_downloads_enabled", True),
            html_media_resolver_enabled=getattr(self.config, "html_media_resolver_enabled", True),
        )

    def _build_telegram_handler(self):
        """Return a Telegram handler for health checks."""
        if self.telegram is not None:
            return self.telegram
        from telegram_handler import TelegramHandler

        return TelegramHandler(self.config.bot_token)

    def _build_media_handler(self):
        """Return a media handler for ffmpeg/ffprobe health checks."""
        if self.media is not None:
            return self.media
        from media_handler import MediaHandler

        return MediaHandler(
            self.config.user_agent,
            getattr(self.config, "max_download_mb", 45),
            domain_downloaders_enabled=getattr(self.config, "domain_downloaders_enabled", True),
            imgur_album_downloads_enabled=getattr(self.config, "imgur_album_downloads_enabled", True),
            html_media_resolver_enabled=getattr(self.config, "html_media_resolver_enabled", True),
        )

    def _check_config_health(self) -> Tuple[bool, str]:
        """Validate configuration."""
        try:
            valid, error = self.config.validate()
        except Exception as exc:
            return False, self._trim_health_text(exc)
        return bool(valid), error or "Configuration is valid"

    def _check_telegram_health(self) -> Tuple[bool, str]:
        """Check Telegram API reachability without sending a message."""
        if not getattr(self.config, "bot_token", ""):
            return False, "Bot token is missing"
        try:
            telegram = self._build_telegram_handler()
            result = telegram.get_me(timeout=12)
            user = result.get("result", {}) if isinstance(result, dict) else {}
            username = str(user.get("username") or "unknown").strip()
            return True, f"Bot @{username} reachable"
        except Exception as exc:
            return False, self._trim_health_text(exc)

    def _check_reddit_health(self) -> Tuple[bool, str]:
        """Check Reddit API reachability against the first configured subreddit."""
        subreddits = list(getattr(self.config, "subreddits", []) or [])
        if not subreddits:
            return False, "No subreddits configured"
        subreddit = str(subreddits[0] or "").strip()
        if not subreddit:
            return False, "First subreddit is empty"
        try:
            reddit = self._build_reddit_handler()
            payload = reddit.fetch_subreddit_new(subreddit, limit=1, timeout=12)
            children = payload.get("data", {}).get("children", []) if isinstance(payload, dict) else []
            count = len(children) if isinstance(children, list) else 0
            return True, f"r/{subreddit} reachable ({count} item{'s' if count != 1 else ''})"
        except Exception as exc:
            return False, self._trim_health_text(exc)

    def _check_ffmpeg_health(self) -> Tuple[bool, str]:
        """Check ffmpeg and ffprobe availability."""
        details: List[str] = []
        ok = True

        try:
            media = self._build_media_handler()
            ffmpeg_ok = bool(getattr(media, "has_ffmpeg", False))
            ffprobe_ok = bool(getattr(media, "has_ffprobe", False))
        except Exception:
            ffmpeg_ok = False
            ffprobe_ok = False

        if not ffmpeg_ok:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-version"],
                    capture_output=True,
                    timeout=5,
                )
                ffmpeg_ok = result.returncode == 0
            except Exception:
                ffmpeg_ok = False
        if not ffprobe_ok:
            try:
                result = subprocess.run(
                    ["ffprobe", "-version"],
                    capture_output=True,
                    timeout=5,
                )
                ffprobe_ok = result.returncode == 0
            except Exception:
                ffprobe_ok = False

        details.append(f"ffmpeg {'available' if ffmpeg_ok else 'missing'}")
        details.append(f"ffprobe {'available' if ffprobe_ok else 'missing'}")
        ok = ffmpeg_ok and ffprobe_ok
        return ok, ", ".join(details)

    def _queue_health_detail(self) -> str:
        """Return queue and pending preview health detail."""
        queue_count = self.state.get_post_queue_count()
        pending_text = "pending preview" if self.state.has_pending() else "no pending preview"
        return f"{queue_count} queued, {pending_text}"

    def _last_successful_post_detail(self) -> str:
        """Return a compact summary of the most recent successful post."""
        try:
            analytics = self.state.get_post_analytics(limit=1)
        except Exception:
            analytics = []
        if analytics:
            record = analytics[0]
            subreddit = str(record.get("subreddit") or "?").strip()
            media_type = str(record.get("type") or "?").strip()
            posted_at = self._format_utc_timestamp(record.get("posted_at_utc"))
            message_id = record.get("message_id")
            suffix = f", message {message_id}" if message_id else ""
            return f"r/{subreddit} [{media_type}] at {posted_at}{suffix}"

        history = self.state.get_history(limit=1)
        if history:
            post = history[-1]
            subreddit = str(post.get("subreddit") or "?").strip()
            media_type = str(post.get("type") or "?").strip()
            posted_at = self._format_utc_timestamp(post.get("timestamp") or post.get("posted_at_utc"))
            return f"r/{subreddit} [{media_type}] at {posted_at}"

        return "none recorded"

    def build_health_report(self, *, include_live_checks: bool = True) -> str:
        """Build a health check report for Telegram and CLI output."""
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        checks: List[Tuple[str, bool, str]] = []

        config_ok, config_detail = self._check_config_health()
        checks.append(("Config", config_ok, config_detail))

        if include_live_checks:
            telegram_ok, telegram_detail = self._check_telegram_health()
            reddit_ok, reddit_detail = self._check_reddit_health()
        else:
            telegram_ok, telegram_detail = True, "Live check skipped"
            reddit_ok, reddit_detail = True, "Live check skipped"
        checks.append(("Telegram", telegram_ok, telegram_detail))
        checks.append(("Reddit", reddit_ok, reddit_detail))

        ffmpeg_ok, ffmpeg_detail = self._check_ffmpeg_health()
        checks.append(("ffmpeg", ffmpeg_ok, ffmpeg_detail))

        overall_ok = all(ok for _, ok, _ in checks)
        lines = ["Health Check", f"Generated: {generated}", ""]
        lines.append(f"Overall: {'OK' if overall_ok else 'Needs attention'}")
        lines.append("")
        for name, ok, detail in checks:
            lines.append(f"{name}: {self._format_health_status(ok, detail)}")

        lines.append(f"Queue: {self._queue_health_detail()}")

        last_error = self.state.get_last_error()
        if last_error:
            error_time = self._format_utc_timestamp(last_error.get("timestamp_utc"))
            error_message = self._trim_health_text(last_error.get("message"), 140)
            lines.append(f"Last error: {error_message} ({error_time})")
        else:
            lines.append("Last error: none")

        lines.append(f"Last successful post: {self._last_successful_post_detail()}")
        return "\n".join(lines)

    def build_activity_summary_report(self) -> str:
        """Build an on-demand private admin activity summary."""
        local_dt = self._local_datetime()
        date_key = local_dt.date().isoformat()
        timezone_name = str(getattr(self.config, "timezone", "UTC") or "UTC")

        posts_today = self.state.get_daily_posts_count(date_key)
        skips_today = self.state.get_daily_skips_count(date_key)
        failures_today = self.state.get_daily_failures_count(date_key)
        limit = self.scheduler.get_effective_daily_limit()
        queue_count = self.state.get_post_queue_count()
        pending = self.state.get_pending() if self.state.has_pending() else None
        summary = self.state.get_analytics_summary(days=1)

        lines = ["Admin Activity Summary"]
        lines.append(f"Date: {local_dt:%Y-%m-%d %H:%M} ({timezone_name})")
        lines.append("")
        if limit > 0:
            lines.append(f"Posts sent: {posts_today}/{limit}")
        else:
            lines.append(f"Posts sent: {posts_today}")
        lines.append(f"Skips: {skips_today}")
        lines.append(f"Failures: {failures_today}")
        lines.append(f"Queue size: {queue_count}")
        if pending:
            lines.append(f"Pending preview: r/{pending.get('subreddit', '?')} ({pending.get('id', '?')})")
        else:
            lines.append("Pending preview: none")

        lines.append("")
        lines.append(f"Next schedule: {self._format_next_schedule_line(posts_today)}")

        source_rows = []
        for item in summary.get("subreddits", [])[:5]:
            source_rows.append(f"r/{item.get('key', '?')} ({item.get('count', 0)})")
        lines.append("")
        if source_rows:
            lines.append("Best sources (24h): " + ", ".join(source_rows))
        else:
            lines.append("Best sources (24h): none yet")

        if summary.get("view_records") or summary.get("reaction_records") or summary.get("forward_records"):
            metric_parts = []
            if summary.get("view_records"):
                metric_parts.append(f"{summary.get('total_views', 0)} views")
            if summary.get("reaction_records"):
                metric_parts.append(f"{summary.get('total_reactions', 0)} reactions")
            if summary.get("forward_records"):
                metric_parts.append(f"{summary.get('total_forwards', 0)} forwards")
            lines.append("Known Telegram metrics: " + ", ".join(metric_parts))
        else:
            lines.append("Known Telegram metrics: waiting for channel updates")

        recent = self.state.get_history(limit=3)
        if recent:
            lines.append("")
            lines.append("Recent posts:")
            for item in recent:
                lines.append(f"  - r/{item.get('subreddit', '?')} ({item.get('type', '?')})")

        last_error = self.state.get_last_error()
        if last_error:
            lines.append("")
            lines.append(f"Last error: {last_error.get('message', 'unknown')}")

        return "\n".join(lines)

    def cmd_start(self, args: str, is_admin: bool = False) -> str:
        """Handle /start command"""
        if not is_admin:
            return (
                "👋 <b>Welcome to Telegram Autoposter</b>\n\n"
                "This bot powers automated channel posting.\n"
                "Admin controls are available in the configured admin chat."
            )

        return (
            "👋 Telegram Autoposter Admin Bot\n\n"
            "This chat is your admin control chat for the autoposter.\n\n"
            "What happens here:\n\n"
            "• You receive preview posts for approval or skipping\n"
            "• The control flow is intentionally simple: Post Now or Skip\n"
            "Quick admin commands:\n\n"
            "/skip – Skip the current pending post\n"
            "/menu – Open the two-button control panel\n"
            "/help – Show all available commands"
        )

    def cmd_help(self, args: str, is_admin: bool = False) -> str:
        """Handle /help command"""
        if not is_admin:
            return (
                "📚 <b>Telegram Autoposter Help</b>\n\n"
                "This bot is used for automated channel posting.\n"
                "If you are the admin, use the configured admin chat for controls."
            )

        return (
            "📚 Telegram Autoposter Admin Commands:\n\n"
            "/start - Admin welcome message\n"
            "/menu - Open the Post Now / Skip control panel\n"
            "/skip - Skip the current pending post\n"
            "/help - This help message\n\n"
            "Preview messages also only show Post Now and Skip."
        )

    def cmd_menu(self, args: str, is_admin: bool = False) -> str:
        """Handle /menu fallback when called outside the bot wrapper."""
        if not is_admin:
            return "Admin controls are available in the configured admin chat."
        return "🤖 Use /menu in Telegram to open the editable admin control panel."

    def cmd_status(self, args: str) -> str:
        """Handle /status command"""
        lines = ["📊 Bot Status\n"]

        # Running state
        if self.state.is_paused():
            lines.append("⏸️ Status: PAUSED")
            emergency_pause = self.state.get_emergency_pause()
            if emergency_pause:
                lines.append(
                    "🚨 Emergency pause: "
                    f"{emergency_pause.get('category', 'unknown')} "
                    f"{emergency_pause.get('count', 0)}/{emergency_pause.get('threshold', 0)}"
                )
        else:
            lines.append("✅ Status: Running")

        # Pending item
        if self.state.has_pending():
            pending = self.state.get_pending()
            lines.append(f"📝 Pending: Yes (r/{pending.get('subreddit')})")

            deadline = pending.get("deadline_utc")
            if deadline:
                try:
                    deadline_dt = datetime.fromisoformat(deadline)
                    now = datetime.now(timezone.utc)
                    remaining = (deadline_dt - now).total_seconds()
                    if remaining > 0:
                        lines.append(f"⏱️ Auto-post in: {format_seconds(int(remaining))}")
                    else:
                        lines.append("⏱️ Auto-post: Now!")
                except Exception:
                    pass
        else:
            lines.append("📝 Pending: No")

        queue_count = self.state.get_post_queue_count()
        lines.append(f"📦 Queue: {queue_count} approved post(s)")

        block_counts = self.state.get_block_counts()
        if any(block_counts.values()):
            lines.append(
                "Blocks: "
                f"{block_counts['authors']} author(s), "
                f"{block_counts['subreddits']} subreddit(s), "
                f"{block_counts['keywords']} keyword(s)"
            )

        # Next post time
        last_tick = self.state.get_last_tick()
        if not self.state.is_paused():
            next_time = self.scheduler.calculate_next_post_time(last_tick)
            now = datetime.now(timezone.utc)

            if next_time > now:
                wait_seconds = (next_time - now).total_seconds()
                lines.append(f"⏰ Next post: {format_seconds(int(wait_seconds))}")
            else:
                lines.append("⏰ Next post: Ready now!")

        # Daily posts
        daily = self.state.get_daily_posts_count(self._local_date_key())
        limit = self.scheduler.get_effective_daily_limit()
        if limit > 0:
            lines.append(f"📅 Today: {daily}/{limit} posts")
        else:
            lines.append(f"📅 Today: {daily} posts")

        schedule = self.scheduler.get_effective_schedule()
        if getattr(self.config, "weekly_schedule_enabled", False):
            schedule_name = schedule.get("schedule_key", "global")
            if schedule.get("paused"):
                lines.append(f"🗓️ Weekly schedule: {schedule_name} paused")
            elif self.scheduler.is_within_active_hours():
                interval = schedule.get("post_interval_minutes", self.config.post_interval_minutes)
                peak = " peak" if schedule.get("peak_active") else ""
                lines.append(f"🗓️ Weekly schedule: {schedule_name}{peak}, {interval} min")
            else:
                next_active = self.scheduler.get_next_active_time()
                if next_active:
                    lines.append(f"🗓️ Weekly schedule: inactive until {next_active.astimezone():%a %H:%M}")
                else:
                    lines.append("🗓️ Weekly schedule: inactive")
        elif self.config.active_hours_enabled:
            if self.scheduler.is_within_active_hours():
                lines.append("🕒 Active hours: Currently active")
            else:
                lines.append(f"🕒 Active hours: Inactive (paused until {self.config.active_hours_start})")

        return "\n".join(lines)

    def cmd_stats(self, args: str) -> str:
        """Handle /stats command"""
        stats = self.state.get_stats()

        lines = ["📈 Statistics\n"]
        lines.append(f"📊 Total posts: {stats.get('total_posts', 0)}")
        lines.append(f"✅ Approved: {stats.get('total_approvals', 0)}")
        lines.append(f"⏭️ Skipped: {stats.get('total_skips', 0)}")

        approval_rate = self.state.get_approval_rate()
        lines.append(f"📊 Approval rate: {approval_rate:.1f}%")

        lines.append(f"\n💾 Seen posts: {len(self.state.get_seen_posts())}")
        lines.append(f"🖼️ Current image streak: {self.state.get_img_streak()}")

        # Recent history
        history = self.state.get_history(5)
        if history:
            lines.append("\n🧾 Last 5 posts:")
            for post in history:
                sr = post.get("subreddit", "?")
                ptype = post.get("type", "?")
                emoji = "🖼️" if ptype == "image" else "🎥"
                lines.append(f"  {emoji} r/{sr}")

        return "\n".join(lines)

    def cmd_analytics(self, args: str) -> str:
        """Handle /analytics command."""
        raw_args = (args or "").strip().split()
        days = 7
        if raw_args:
            try:
                days = int(raw_args[0])
            except ValueError:
                return "Usage: /analytics [days]"

        return self.state.build_analytics_report(days=days, limit=5)

    def cmd_digest(self, args: str) -> str:
        """Handle /digest command."""
        return self.build_activity_summary_report()

    def cmd_health(self, args: str) -> str:
        """Handle /health command."""
        raw = (args or "").strip().lower()
        include_live_checks = raw not in {"quick", "local", "no-live", "offline"}
        return self.build_health_report(include_live_checks=include_live_checks)

    def _parse_error_limit(self, raw: str, default: int = 10) -> int:
        """Parse a report limit from command args."""
        for token in (raw or "").strip().split():
            try:
                return max(1, min(int(token), 50))
            except ValueError:
                continue
        return default

    def build_error_report(
        self,
        *,
        limit: int = 10,
        include_runtime_logs: bool = True,
    ) -> str:
        """Build a recent error report for Telegram and CLI output."""
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 10

        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        errors = self.state.get_recent_errors(limit)
        lines = ["Recent Errors", f"Generated: {generated}", ""]

        if errors:
            lines.append(f"Persistent failures (newest {len(errors)}):")
            for event in errors:
                timestamp = self._format_utc_timestamp(event.get("timestamp_utc"))
                category = str(event.get("category") or "runtime").strip() or "runtime"
                message = self._trim_health_text(event.get("message"), 120)
                source = str(event.get("source") or "").strip()
                source_text = f" | {source}" if source and source != "record_failure" else ""
                lines.append(f"  - {timestamp} | {category}{source_text} | {message}")
        else:
            lines.append("Persistent failures: none recorded")

        if include_runtime_logs:
            runtime_logs = [
                item for item in get_log_history(80) if str(item.get("level") or "").upper() in {"WARN", "ERROR"}
            ][: min(limit, 10)]
            lines.append("")
            if runtime_logs:
                lines.append(f"Current session WARN/ERROR logs (newest {len(runtime_logs)}):")
                for item in runtime_logs:
                    timestamp = str(item.get("timestamp") or "unknown time")[:19]
                    level = str(item.get("level") or "?").upper()
                    thread = str(item.get("thread") or "").strip()
                    thread_text = f" | {thread}" if thread and thread != "MainThread" else ""
                    message = self._trim_health_text(item.get("message"), 120)
                    lines.append(f"  - {timestamp} | {level}{thread_text} | {message}")
            else:
                lines.append("Current session WARN/ERROR logs: none")

        lines.append("")
        lines.append("Use /errors 20 for a larger view or /errors clear to clear stored error history.")
        return "\n".join(lines)

    def cmd_errors(self, args: str) -> str:
        """Handle /errors command."""
        raw = (args or "").strip().lower()
        if raw in {"clear", "reset"}:
            cleared = self.state.clear_recent_errors()
            self.state.save()
            log(f"Cleared {cleared} stored error record(s) by admin command")
            return f"Cleared {cleared} stored error record(s). Emergency pause counters were not reset."

        if raw in {"help", "?"}:
            return "Usage: /errors [count] or /errors clear"

        limit = self._parse_error_limit(raw)
        return self.build_error_report(limit=limit)

    def build_recent_logs_report(self, limit: int = 15) -> str:
        """Build a runtime log report for recent terminal output."""
        try:
            limit_value = max(1, min(int(limit), 25))
        except (TypeError, ValueError):
            limit_value = 15

        entries = get_log_history(limit_value)
        lines = ["Recent Logs", ""]
        if not entries:
            lines.append("No runtime logs captured yet.")
            return "\n".join(lines)

        for item in reversed(entries):
            timestamp = str(item.get("timestamp") or "unknown time")[:19]
            level = str(item.get("level") or "?").upper()
            thread = str(item.get("thread") or "").strip()
            thread_text = f" | {thread}" if thread and thread != "MainThread" else ""
            message = self._trim_health_text(item.get("message"), 140)
            lines.append(f"- {timestamp} | {level}{thread_text} | {message}")
        return "\n".join(lines)

    def cmd_logs(self, args: str) -> str:
        """Handle /logs command."""
        limit = self._parse_error_limit(args, default=15)
        return self.build_recent_logs_report(limit=limit)

    def build_auto_recovery_report(self, *, limit: int = 8) -> str:
        """Build an auto-recovery status report."""
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 8

        window = max(1, int(getattr(self.config, "auto_recovery_notify_window_minutes", 30) or 30))
        failures = self.state.get_recovery_failure_count(window)
        events = self.state.get_recovery_events(limit)
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        last_notice = self.state.get_recovery_last_notice_utc()

        lines = ["Auto-Recovery", f"Generated: {generated}", ""]
        lines.append(f"Status: {'On' if getattr(self.config, 'auto_recovery_enabled', True) else 'Off'}")
        lines.append(
            "Upload retries: "
            f"{int(getattr(self.config, 'auto_recovery_upload_retries', 2) or 0)} "
            f"with {int(getattr(self.config, 'auto_recovery_retry_delay_seconds', 5) or 0)}s delay"
        )
        lines.append(
            "Compression fallback: "
            f"{'On' if getattr(self.config, 'auto_recovery_compress_on_retry', True) else 'Off'} "
            f"(video {int(getattr(self.config, 'auto_recovery_video_target_mb', 30) or 30)} MB, "
            f"image {int(getattr(self.config, 'auto_recovery_image_target_mb', 8) or 8)} MB)"
        )
        lines.append(
            f"Stuck pending skip: {int(getattr(self.config, 'auto_recovery_stuck_pending_minutes', 90) or 90)} min"
        )
        lines.append(
            "Admin alert: "
            f"{int(getattr(self.config, 'auto_recovery_notify_threshold', 3) or 3)} "
            f"failure(s) / {window} min, "
            f"{int(getattr(self.config, 'auto_recovery_notify_cooldown_minutes', 30) or 30)} min cooldown"
        )
        lines.append(
            f"Recent failure count: {failures}/{int(getattr(self.config, 'auto_recovery_notify_threshold', 3) or 3)}"
        )
        lines.append(f"Last admin alert: {self._format_utc_timestamp(last_notice) if last_notice else 'none'}")

        if events:
            lines.append("")
            lines.append(f"Recent events (newest {len(events)}):")
            for event in events:
                timestamp = self._format_utc_timestamp(event.get("timestamp_utc"))
                action = str(event.get("action") or "?").strip()
                level = str(event.get("level") or "?").upper()
                post_id = str(event.get("post_id") or "").strip()
                post_text = f" | {post_id}" if post_id else ""
                detail = self._trim_health_text(event.get("detail"), 110)
                lines.append(f"  - {timestamp} | {level} | {action}{post_text} | {detail}")
        else:
            lines.append("")
            lines.append("Recent events: none")

        lines.append("")
        lines.append("Use /recovery clear to clear auto-recovery event history.")
        return "\n".join(lines)

    def cmd_recovery(self, args: str) -> str:
        """Handle /recovery command."""
        raw = (args or "").strip().lower()
        if raw in {"clear", "reset"}:
            cleared = self.state.clear_recovery_events()
            self.state.save()
            log(f"Cleared {cleared} auto-recovery event(s) by admin command")
            return f"Cleared {cleared} auto-recovery event(s)."
        if raw in {"help", "?"}:
            return "Usage: /recovery [count] or /recovery clear"
        limit = self._parse_error_limit(raw, default=8)
        return self.build_auto_recovery_report(limit=limit)

    def _emergency_thresholds(self) -> Dict[str, int]:
        """Return emergency pause thresholds by category."""
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

    def build_emergency_pause_report(self) -> str:
        """Build a report for emergency pause rules and recent failures."""
        enabled = bool(getattr(self.config, "emergency_pause_enabled", True))
        window = max(1, int(getattr(self.config, "emergency_pause_window_minutes", 30) or 30))
        thresholds = self._emergency_thresholds()
        counts = self.state.get_emergency_failure_counts(window)
        active_pause = self.state.get_emergency_pause()

        lines = ["Emergency Pause\n"]
        lines.append(f"Status: {'On' if enabled else 'Off'}")
        lines.append(f"Window: {window} min")
        lines.append(f"Admin alert: {'On' if getattr(self.config, 'emergency_pause_notify_admin', True) else 'Off'}")
        if active_pause:
            lines.append("")
            lines.append(
                "Active pause: "
                f"{active_pause.get('category', 'unknown')} "
                f"{active_pause.get('count', 0)}/{active_pause.get('threshold', 0)}"
            )
            reason = str(active_pause.get("reason") or "").strip()
            if reason:
                lines.append(f"Reason: {reason}")
            triggered = str(active_pause.get("triggered_at_utc") or "").replace("T", " ")[:19]
            if triggered:
                lines.append(f"Triggered UTC: {triggered}")
        else:
            lines.append("Active pause: none")

        lines.append("")
        lines.append("Failure counters:")
        for category in ("reddit", "telegram", "download", "empty_feed"):
            threshold = thresholds.get(category, 0)
            threshold_text = "off" if threshold <= 0 else str(threshold)
            lines.append(f"  {category}: {counts.get(category, 0)}/{threshold_text}")

        recent = self.state.get_emergency_failures(window)[:5]
        if recent:
            lines.append("")
            lines.append("Recent failures:")
            for event in recent:
                ts = str(event.get("timestamp_utc") or "").replace("T", " ")[:19]
                reason = str(event.get("reason") or "").strip()
                if len(reason) > 70:
                    reason = reason[:67] + "..."
                lines.append(f"  - {ts} | {event.get('category', '?')} | {reason}")

        lines.append("")
        lines.append("Use /emergency reset to clear emergency history and resume posting.")
        return "\n".join(lines)

    def cmd_emergency(self, args: str) -> str:
        """Handle /emergency command."""
        action = (args or "").strip().lower()
        if action in {"reset", "clear", "resume"}:
            self.state.clear_emergency_pause()
            self.state.clear_emergency_failures()
            self.state.set_paused(False)
            self.state.save()
            log("Emergency pause state cleared by admin command")
            return "Emergency pause history cleared. Posting is resumed."

        return self.build_emergency_pause_report()

    def cmd_traffic(self, args: str) -> str:
        """Handle /traffic command"""
        if not self.traffic:
            return "❌ Traffic analytics are not available right now."

        return self.traffic.build_report()

    def _format_queue_item(self, index: int, item: Dict[str, Any]) -> str:
        """Format a queue item for Telegram."""
        title = str(item.get("title") or "").strip() or "(untitled)"
        if len(title) > 58:
            title = title[:55] + "..."

        subreddit = item.get("subreddit") or "?"
        media_type = item.get("type") or "?"
        return f"{index}. r/{subreddit} [{media_type}] {title}"

    def cmd_queue(self, args: str) -> str:
        """Handle /queue command."""
        raw_args = (args or "").strip()
        parts = raw_args.split()
        action = parts[0].lower() if parts else "list"

        if action in {"list", "show"}:
            queue = self.state.get_post_queue()
            if not queue:
                return "📦 Post Queue\n\nQueue is empty.\nQueue controls are disabled in simplified approval mode."

            lines = ["📦 Post Queue", f"{len(queue)} approved item(s)\n"]
            for index, item in enumerate(queue[:10], 1):
                lines.append(self._format_queue_item(index, item))
            if len(queue) > 10:
                lines.append(f"...and {len(queue) - 10} more.")

            lines.append("\nCommands: /queue post, /queue remove N, /queue up N, /queue down N, /queue clear")
            return "\n".join(lines)

        if action in {"post", "next"}:
            if self.state.has_pending():
                return "⚠️ Approve, queue, or skip the current pending preview first."
            if not self.state.has_queued_posts():
                return "📦 Queue is empty."

            self.state.set_last_tick(None)
            self.state.save()
            return "⏩ Next queued post will be posted immediately."

        if action in {"remove", "rm", "delete"}:
            if len(parts) < 2 or not parts[1].isdigit():
                return "Usage: /queue remove N"

            index = int(parts[1])
            removed = self.state.remove_queued_post(index)
            if not removed:
                return "Queue item not found."

            self.state.save()
            return f"Removed queued item {index}: r/{removed.get('subreddit', '?')}"

        if action in {"up", "down"}:
            if len(parts) < 2 or not parts[1].isdigit():
                return f"Usage: /queue {action} N"

            index = int(parts[1])
            direction = -1 if action == "up" else 1
            if not self.state.move_queued_post(index, direction):
                return "Queue item cannot be moved that way."

            self.state.save()
            return f"Moved queue item {index} {action}."

        if action == "clear":
            count = self.state.clear_post_queue()
            self.state.save()
            return f"Cleared {count} queued item(s)."

        return (
            "Unknown queue command.\n"
            "Use: /queue, /queue post, /queue remove N, /queue up N, "
            "/queue down N, /queue clear"
        )

    def _format_block_values(self, values, *, prefix: str = "") -> str:
        """Format a compact block-list preview."""
        if not values:
            return "none"

        display = [f"{prefix}{value}" for value in values[:12]]
        text = ", ".join(display)
        if len(values) > 12:
            text += f", ...and {len(values) - 12} more"
        return text

    def cmd_blocks(self, args: str) -> str:
        """Handle /blocks command."""
        raw_args = (args or "").strip()
        parts = raw_args.split()

        if parts and parts[0].lower() == "clear":
            target = parts[1].lower() if len(parts) > 1 else "all"
            count = self.state.clear_blocklist(target)
            if count is None:
                return (
                    "Unknown block list.\n"
                    "Use: /blocks clear authors, /blocks clear subreddits, "
                    "/blocks clear keywords, or /blocks clear all"
                )

            self.state.save()
            return f"Cleared {count} blocked item(s)."

        authors = self.state.list_blocked_authors()
        subreddits = self.state.list_blocked_subreddits()
        keywords = self.state.list_blocked_title_keywords()

        lines = ["Admin Blocks\n"]
        lines.append(f"Authors ({len(authors)}): {self._format_block_values(authors, prefix='u/')}")
        lines.append(f"Subreddits ({len(subreddits)}): {self._format_block_values(subreddits, prefix='r/')}")
        lines.append(f"Title keywords ({len(keywords)}): {self._format_block_values(keywords)}")
        lines.append(
            "\nUse: /blocks clear authors, /blocks clear subreddits, /blocks clear keywords, or /blocks clear all"
        )
        return "\n".join(lines)

    def _format_subreddit_rule(self, subreddit: str, rule: Dict[str, Any]) -> str:
        """Format a configured per-subreddit rule."""
        parts = []
        if "min_upvotes" in rule:
            parts.append(f"min {rule['min_upvotes']} upvotes")
        if "max_post_age_hours" in rule:
            parts.append(f"max age {rule['max_post_age_hours']}h")
        if rule.get("media_type") and rule.get("media_type") != "any":
            parts.append(f"{rule['media_type']} only")
        if "skip_nsfw" in rule:
            parts.append("skip NSFW" if rule["skip_nsfw"] else "allow NSFW")
        if "priority_weight" in rule:
            parts.append(f"weight {rule['priority_weight']:g}")
        if rule.get("caption_footer"):
            footer = str(rule["caption_footer"]).strip()
            if len(footer) > 42:
                footer = footer[:39] + "..."
            parts.append(f"footer '{footer}'")
        if rule.get("caption_template"):
            template = str(rule["caption_template"]).strip()
            if len(template) > 42:
                template = template[:39] + "..."
            parts.append(f"template '{template}'")
        if rule.get("caption_variants"):
            parts.append(f"{len(rule['caption_variants'])} caption variants")

        summary = "; ".join(parts) if parts else "configured"
        return f"r/{subreddit}: {summary}"

    def cmd_rules(self, args: str) -> str:
        """Handle /rules command."""
        rules = getattr(self.config, "subreddit_rules", {}) or {}
        if not rules:
            return (
                "Per-subreddit rules\n\n"
                "No source-specific rules configured.\n"
                "Add them in config.json under subreddit_rules."
            )

        lines = ["Per-subreddit rules", f"{len(rules)} configured source(s)\n"]
        for subreddit in sorted(rules.keys())[:20]:
            lines.append(self._format_subreddit_rule(subreddit, rules[subreddit]))

        if len(rules) > 20:
            lines.append(f"...and {len(rules) - 20} more.")

        lines.append(
            "\nFields: min_upvotes, max_post_age_hours, media_type, "
            "skip_nsfw, caption_template, caption_footer, caption_variants, priority_weight."
        )
        lines.append("media_type values: any, image, video, gallery.")
        return "\n".join(lines)

    def _caption_mode_label(self, mode: str) -> str:
        """Return a readable caption mode label."""
        labels = {
            "template": "Custom caption",
            "source": "Copy Reddit title",
            "source_plus_footer": "Reddit title + custom text",
            "source_plus_body": "Source title + body excerpt",
            "source_with_credit": "Source title + credit",
            "credit_only": "Credit only",
            "none": "No caption",
            "variants": "Rotate caption variants",
        }
        return labels.get(mode, labels["template"])

    def _format_caption_variant(self, index: int, variant: Dict[str, Any]) -> str:
        """Format one caption variant for Telegram."""
        name = str(variant.get("name") or f"Variant {index}").strip()
        mode = str(variant.get("mode") or "template")
        text = f"{index}. {name}: {self._caption_mode_label(mode)}"
        template = str(variant.get("template") or "").strip()
        footer = str(variant.get("footer_template") or "").strip()
        if template:
            if len(template) > 44:
                template = template[:41] + "..."
            text += f" - {template}"
        if footer:
            if len(footer) > 36:
                footer = footer[:33] + "..."
            text += f" - below: {footer}"
        return text

    def cmd_caption(self, args: str) -> str:
        """Handle /caption command."""
        mode = getattr(self.config, "caption_mode", "template")
        variants = list(getattr(self.config, "caption_variants", []) or [])
        rules = getattr(self.config, "subreddit_rules", {}) or {}
        template_rules = [
            subreddit for subreddit, rule in rules.items() if isinstance(rule, dict) and rule.get("caption_template")
        ]
        variant_rules = [
            subreddit for subreddit, rule in rules.items() if isinstance(rule, dict) and rule.get("caption_variants")
        ]

        lines = ["Caption Settings\n"]
        lines.append(f"Mode: {self._caption_mode_label(mode)}")
        lines.append(f"Global variants: {len(variants)}")
        lines.append(f"Per-subreddit templates: {len(template_rules)}")
        lines.append(f"Per-subreddit variant sets: {len(variant_rules)}")

        if variants:
            lines.append("\nGlobal variant rotation:")
            for index, variant in enumerate(variants[:10], 1):
                lines.append(self._format_caption_variant(index, variant))
            if len(variants) > 10:
                lines.append(f"...and {len(variants) - 10} more.")

        if template_rules:
            preview = ", ".join(f"r/{sub}" for sub in sorted(template_rules)[:12])
            if len(template_rules) > 12:
                preview += f", ...and {len(template_rules) - 12} more"
            lines.append(f"\nTemplate overrides: {preview}")

        if variant_rules:
            preview = ", ".join(f"r/{sub}" for sub in sorted(variant_rules)[:12])
            if len(variant_rules) > 12:
                preview += f", ...and {len(variant_rules) - 12} more"
            lines.append(f"Variant overrides: {preview}")

        lines.append("\nEdit captions from the local CLI caption menu.")
        return "\n".join(lines)

    def cmd_dedupe(self, args: str) -> str:
        """Handle /dedupe command."""
        title_enabled = bool(getattr(self.config, "duplicate_title_similarity_enabled", True))
        title_threshold = float(getattr(self.config, "duplicate_title_similarity_threshold", 0.88) or 0.88)
        title_limit = int(getattr(self.config, "duplicate_title_similarity_history_limit", 500) or 500)
        crosspost_enabled = bool(getattr(self.config, "duplicate_crosspost_blocking", True))
        author_enabled = bool(getattr(self.config, "author_cooldown_enabled", False))
        author_hours = int(getattr(self.config, "author_cooldown_hours", 24) or 0)
        author_history = len(getattr(self.state, "state", {}).get("author_history", []) or [])
        title_signatures = len(self.state.get_title_signatures(self.state.get_blocked_signatures()))

        lines = ["Duplicate Detection\n"]
        lines.append("URL normalization: On")
        lines.append(f"Crosspost blocking: {'On' if crosspost_enabled else 'Off'}")
        lines.append(
            f"Title similarity: {'On' if title_enabled else 'Off'} "
            f"(threshold {title_threshold:.2f}, last {title_limit})"
        )
        lines.append(f"Author cooldown: {'On' if author_enabled else 'Off'} ({author_hours}h)")
        lines.append("")
        lines.append(f"Stored title signatures: {title_signatures}")
        lines.append(f"Stored author cooldown entries: {author_history}")
        lines.append(f"Blocked content signatures: {len(self.state.get_blocked_signatures())}")
        lines.append("\nEdit duplicate detection from the local CLI.")
        return "\n".join(lines)

    def cmd_scoring(self, args: str) -> str:
        """Handle /scoring command."""
        enabled = bool(getattr(self.config, "smart_scoring_enabled", True))
        top_pool = int(getattr(self.config, "smart_scoring_top_pool_size", 8) or 8)
        weights = getattr(self.config, "smart_scoring_weights", {}) or {}

        lines = ["Smart Content Scoring\n"]
        lines.append(f"Status: {'On' if enabled else 'Off'}")
        lines.append(f"Top pool: {top_pool}")
        lines.append("Weights:")

        default_order = [
            "upvotes",
            "comments",
            "freshness",
            "media_type",
            "title_quality",
            "subreddit_repetition",
        ]
        for key in default_order:
            value = float(weights.get(key, 0.0) or 0.0)
            lines.append(f"  {key}: {value:g}")

        lines.append(
            "\nScoring ranks candidates by engagement, freshness, media mix, "
            "title quality, source variety, and per-subreddit priority weight."
        )
        return "\n".join(lines)

    def _format_schedule_rule(self, key: str, rule: Dict[str, Any]) -> str:
        """Format a weekly schedule rule for Telegram."""
        parts = []
        if rule.get("paused"):
            parts.append("paused")
        if "post_interval_minutes" in rule:
            parts.append(f"{rule['post_interval_minutes']} min")
        if rule.get("active_hours_enabled"):
            start = rule.get("active_hours_start", "?")
            end = rule.get("active_hours_end", "?")
            parts.append(f"active {start}-{end}")
        if "daily_post_limit" in rule:
            limit = rule["daily_post_limit"]
            parts.append("unlimited/day" if not limit else f"{limit}/day")
        if rule.get("quiet_hours"):
            parts.append(f"{len(rule['quiet_hours'])} quiet")
        if rule.get("peak_hours"):
            parts.append(f"{len(rule['peak_hours'])} peak")
        return f"{key}: {', '.join(parts) if parts else 'configured'}"

    def cmd_schedule(self, args: str) -> str:
        """Handle /schedule command."""
        enabled = bool(getattr(self.config, "weekly_schedule_enabled", False))
        rules = getattr(self.config, "weekly_schedule", {}) or {}
        current = self.scheduler.get_effective_schedule()

        lines = ["Weekly Schedule\n"]
        lines.append(f"Status: {'On' if enabled else 'Off'}")
        lines.append(f"Timezone: {getattr(self.config, 'timezone', 'UTC')}")
        lines.append(f"Current rule: {current.get('schedule_key', 'global')}")
        if current.get("paused"):
            lines.append("Current state: paused")
        elif self.scheduler.is_within_active_hours():
            peak = " (peak)" if current.get("peak_active") else ""
            lines.append(f"Current state: active{peak}")
        else:
            next_active = self.scheduler.get_next_active_time()
            if next_active:
                lines.append(f"Current state: inactive until {next_active.astimezone():%a %H:%M}")
            else:
                lines.append("Current state: inactive")

        lines.append(f"Interval now: {current.get('post_interval_minutes', self.config.post_interval_minutes)} min")
        limit = self.scheduler.get_effective_daily_limit()
        lines.append(f"Daily limit now: {'unlimited' if limit <= 0 else limit}")

        if not rules:
            lines.append("\nNo weekly rules configured.")
            return "\n".join(lines)

        lines.append("\nRules:")
        order = [
            "weekday",
            "weekend",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
        for key in order:
            if key in rules:
                lines.append(self._format_schedule_rule(key, rules[key]))

        return "\n".join(lines)

    def cmd_pause(self, args: str) -> str:
        """Handle /pause command"""
        if self.state.is_paused():
            return "⏸️ Bot is already paused"

        self.state.set_paused(True)
        self.state.save()
        log("Bot paused by admin command")

        return "⏸️ Bot paused. Use /resume to continue posting."

    def cmd_resume(self, args: str) -> str:
        """Handle /resume command"""
        if not self.state.is_paused():
            return "▶️ Bot is already running"

        self.state.set_paused(False)
        self.state.save()
        log("Bot resumed by admin command")

        return "▶️ Bot resumed. Will create posts according to schedule."

    def cmd_skip(self, args: str) -> str:
        """Handle /skip command"""
        if not self.state.has_pending():
            return "❌ No pending post to skip"

        pending = self.state.get_pending()
        post_id = pending.get("id", "unknown")
        post_url = pending.get("url", "")
        post_title = pending.get("title", "")
        permalink = pending.get("permalink", "")
        crosspost_parent = pending.get("crosspost_parent", "")

        # Mark as skipped and clear
        self.state.mark_skipped(
            post_id,
            post_url,
            post_title,
            permalink,
            crosspost_parent,
            post=pending,
        )
        self.state.clear_pending()
        self.state.increment_stat("total_skips")
        self.state.increment_daily_skips(self._local_date_key())
        self.state.save()

        log(f"Post skipped by admin: {post_id}")

        return f"⏭️ Skipped post from r/{pending.get('subreddit')}. Next post will be created on schedule."

    def cmd_next(self, args: str) -> str:
        """Handle /next command"""
        if self.state.has_pending():
            return "⚠️ There's already a pending post. Approve or skip it first."

        # Reset last tick to force immediate creation
        self.state.set_last_tick(None)
        self.state.save()

        log("Next post forced by admin command")

        return "⏩ Next post will be created immediately!"

    def cmd_config(self, args: str) -> str:
        """Handle /config command"""
        lines = ["⚙️ Current Configuration\n"]
        caption_labels = {
            "template": "Custom caption",
            "source": "Copy Reddit title",
            "source_plus_footer": "Reddit title + custom text",
            "source_plus_body": "Source title + body excerpt",
            "source_with_credit": "Source title + credit",
            "credit_only": "Credit only",
            "none": "No caption",
            "variants": "Rotate caption variants",
        }

        lines.append(f"📣 Channel: {self.config.get_default_channel() or 'Not set'}")
        lines.append(
            "👽 Reddit API: " + ("OAuth app-only" if self.config.has_reddit_oauth_credentials() else "Legacy anonymous")
        )
        if self.config.uses_legacy_reddit_user_agent():
            lines.append("🪪 Reddit user-agent: needs review")
        lines.append(f"🔁 Post interval: {self.config.post_interval_minutes} min")
        lines.append(
            f"✍️ Caption: {caption_labels.get(getattr(self.config, 'caption_mode', 'template'), 'Custom caption')}"
        )
        lines.append(f"🧩 Caption variants: {len(getattr(self.config, 'caption_variants', []) or [])}")
        lines.append(f"👍 Reactions: {'On' if getattr(self.config, 'auto_reactions_enabled', True) else 'Off'}")

        if self.config.post_interval_randomize:
            lines.append(f"🎲 Randomize: ±{self.config.randomize_range_minutes} min")

        if self.config.active_hours_enabled:
            lines.append(f"🕒 Active hours: {self.config.active_hours_start} - {self.config.active_hours_end}")

        if getattr(self.config, "weekly_schedule_enabled", False):
            lines.append(f"🗓️ Weekly schedule: On ({len(getattr(self.config, 'weekly_schedule', {}) or {})} rule(s))")
        else:
            lines.append("🗓️ Weekly schedule: Off")

        lines.append(f"⏱️ Auto-approve: {self.config.auto_approve_after_minutes} min")

        if self.config.daily_post_limit > 0:
            lines.append(f"📅 Daily limit: {self.config.daily_post_limit} posts")

        lines.append(f"🙈 Spoiler posts: {'On' if getattr(self.config, 'spoiler_posts_enabled', False) else 'Off'}")
        if getattr(self.config, "image_quality_rules_enabled", True):
            image_parts = [
                f">={getattr(self.config, 'min_image_width', 800)}x{getattr(self.config, 'min_image_height', 0) or 'any'}",
                (
                    f"ratio {float(getattr(self.config, 'image_aspect_ratio_min', 0.20) or 0.20):g}-"
                    f"{float(getattr(self.config, 'image_aspect_ratio_max', 5.00) or 5.00):g}"
                ),
            ]
            if getattr(self.config, "image_blur_filter_enabled", False):
                image_parts.append(f"blur {float(getattr(self.config, 'image_blur_score_min', 35.0) or 35.0):g}+")
            if getattr(self.config, "image_screenshot_filter_enabled", False):
                image_parts.append("no screenshots")
            if getattr(self.config, "image_text_heavy_filter_enabled", False):
                image_parts.append(
                    f"text <= {float(getattr(self.config, 'image_text_heavy_max_edge_density', 0.18) or 0.18):.2f}"
                )
            lines.append(f"🧼 Image quality: {', '.join(image_parts)}")
        else:
            lines.append("🧼 Image quality: Off")
        lines.append(
            "🖼️ Gallery posts: "
            f"{'On' if getattr(self.config, 'gallery_posts_enabled', True) else 'Off'} "
            f"({getattr(self.config, 'min_gallery_items', 2)}-"
            f"{getattr(self.config, 'max_gallery_items', 6)} items)"
        )
        video_duration = int(getattr(self.config, "max_video_length_seconds", 0) or 0)
        video_duration_text = f"{video_duration}s" if video_duration > 0 else "unlimited"
        video_audio = str(getattr(self.config, "video_audio_policy", "allow_silent") or "allow_silent").replace(
            "_", " "
        )
        video_orientation = getattr(self.config, "video_orientation_rule", "any")
        video_convert = "mp4" if getattr(self.config, "video_convert_to_mp4", True) else "original"
        video_compress = (
            f"{int(getattr(self.config, 'video_compression_target_mb', 40) or 40)}MB"
            if getattr(self.config, "video_compression_enabled", True)
            else "off"
        )
        lines.append(
            "🎞️ Video rules: "
            f"{video_duration_text}, {video_orientation}, {video_audio}, {video_convert}, compression {video_compress}"
        )
        lines.append(
            "🌐 Domain downloaders: "
            f"{'On' if getattr(self.config, 'domain_downloaders_enabled', True) else 'Off'} "
            f"(imgur {'On' if getattr(self.config, 'imgur_album_downloads_enabled', True) else 'Off'}, "
            f"pages {'On' if getattr(self.config, 'html_media_resolver_enabled', True) else 'Off'})"
        )

        rule_count = len(getattr(self.config, "subreddit_rules", {}) or {})
        lines.append(f"📌 Per-subreddit rules: {rule_count}")
        lines.append(f"🎯 Smart scoring: {'On' if getattr(self.config, 'smart_scoring_enabled', True) else 'Off'}")
        lines.append(
            "🧬 Duplicate detection: "
            f"title {'On' if getattr(self.config, 'duplicate_title_similarity_enabled', True) else 'Off'}, "
            f"xpost {'On' if getattr(self.config, 'duplicate_crosspost_blocking', True) else 'Off'}, "
            f"author cooldown {'On' if getattr(self.config, 'author_cooldown_enabled', False) else 'Off'}"
        )
        lines.append(
            "🚨 Emergency pause: "
            f"{'On' if getattr(self.config, 'emergency_pause_enabled', True) else 'Off'} "
            f"({getattr(self.config, 'emergency_pause_window_minutes', 30)}m window)"
        )
        lines.append(
            "🛠️ Auto-recovery: "
            f"{'On' if getattr(self.config, 'auto_recovery_enabled', True) else 'Off'} "
            f"({int(getattr(self.config, 'auto_recovery_upload_retries', 2) or 0)} upload retries)"
        )

        lines.append("\n🎯 Filters:")
        if self.config.min_upvotes > 0:
            lines.append(f"  ⬆️ Min upvotes: {self.config.min_upvotes}")
        if self.config.max_post_age_hours > 0:
            lines.append(f"  ⏰ Max age: {self.config.max_post_age_hours}h")
        if self.config.skip_nsfw:
            lines.append("  🔞 Skip NSFW: Yes")

        return "\n".join(lines)

    def cmd_subs(self, args: str) -> str:
        """Handle /subs command"""
        lines = ["📑 Active Subreddits\n"]

        for i, sub in enumerate(self.config.subreddits, 1):
            get_rule = getattr(self.config, "get_subreddit_rule", None)
            marker = " rule" if callable(get_rule) and get_rule(sub) else ""
            lines.append(f"{i}. r/{sub}{marker}")

        lines.append(f"\n📊 Total: {len(self.config.subreddits)} subreddits")

        return "\n".join(lines)
