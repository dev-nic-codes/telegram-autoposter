"""
Scheduling logic for posting.
Handles timing, active hours, and rate limiting.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import random
from utils import log, parse_time


class Scheduler:
    """Manages posting schedule and timing"""

    def __init__(self, config):
        """
        Initialize scheduler with configuration.

        Args:
            config: Config object with schedule settings
        """
        self.config = config

    def _local_now(self, when: Optional[datetime] = None) -> datetime:
        """Return a timezone-aware local schedule datetime."""
        base = when or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)

        timezone_name = str(getattr(self.config, "timezone", "UTC") or "UTC").strip()
        try:
            tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            if timezone_name.upper() != "UTC":
                log(f"Invalid timezone '{timezone_name}', using UTC", "WARN")
            tz = timezone.utc

        return base.astimezone(tz)

    def _time_in_range(self, current_minutes: int, start: str, end: str) -> bool:
        """Return True if current local minutes fall inside a HH:MM range."""
        start_time = parse_time(start)
        end_time = parse_time(end)
        if not start_time or not end_time:
            return False

        start_total = start_time[0] * 60 + start_time[1]
        end_total = end_time[0] * 60 + end_time[1]

        if start_total > end_total:
            return current_minutes >= start_total or current_minutes <= end_total
        return start_total <= current_minutes <= end_total

    def _is_in_ranges(self, local_dt: datetime, ranges: List[Dict[str, Any]]) -> bool:
        """Return True if local_dt is inside any configured time range."""
        current_minutes = local_dt.hour * 60 + local_dt.minute
        for item in ranges:
            if self._time_in_range(
                current_minutes,
                str(item.get("start", "")),
                str(item.get("end", "")),
            ):
                return True
        return False

    def get_effective_schedule(self, when: Optional[datetime] = None) -> Dict[str, Any]:
        """Return global schedule settings merged with weekly overrides."""
        local_dt = self._local_now(when)
        effective: Dict[str, Any] = {
            "paused": False,
            "post_interval_minutes": int(getattr(self.config, "post_interval_minutes", 45) or 45),
            "post_interval_randomize": bool(getattr(self.config, "post_interval_randomize", True)),
            "randomize_range_minutes": int(getattr(self.config, "randomize_range_minutes", 0) or 0),
            "active_hours_enabled": bool(getattr(self.config, "active_hours_enabled", False)),
            "active_hours_start": str(getattr(self.config, "active_hours_start", "08:00") or "08:00"),
            "active_hours_end": str(getattr(self.config, "active_hours_end", "23:00") or "23:00"),
            "daily_post_limit": int(getattr(self.config, "daily_post_limit", 0) or 0),
            "quiet_hours": [],
            "peak_hours": [],
            "schedule_key": "global",
            "local_weekday": local_dt.strftime("%A").lower(),
            "local_time": local_dt.strftime("%H:%M"),
        }

        if not bool(getattr(self.config, "weekly_schedule_enabled", False)):
            return effective

        schedule = getattr(self.config, "weekly_schedule", {}) or {}
        group_key = "weekend" if local_dt.weekday() >= 5 else "weekday"
        day_key = local_dt.strftime("%A").lower()

        for key in (group_key, day_key):
            rule = schedule.get(key)
            if not isinstance(rule, dict):
                continue
            for field, value in rule.items():
                if field in {"quiet_hours", "peak_hours"}:
                    effective[field] = list(value or [])
                else:
                    effective[field] = value
            effective["schedule_key"] = key

        current_minutes = local_dt.hour * 60 + local_dt.minute
        for peak in effective.get("peak_hours", []):
            if not isinstance(peak, dict):
                continue
            if self._time_in_range(
                current_minutes,
                str(peak.get("start", "")),
                str(peak.get("end", "")),
            ):
                interval = peak.get("post_interval_minutes")
                if interval:
                    try:
                        effective["post_interval_minutes"] = max(1, int(interval))
                        effective["peak_active"] = True
                        effective["peak_range"] = {
                            "start": peak.get("start"),
                            "end": peak.get("end"),
                        }
                    except (TypeError, ValueError):
                        pass
                break

        return effective

    def get_effective_interval_minutes(self, when: Optional[datetime] = None) -> int:
        """Return the active schedule interval, including peak-hour overrides."""
        schedule = self.get_effective_schedule(when)
        interval_minutes = int(schedule.get("post_interval_minutes", 45) or 45)

        if bool(schedule.get("post_interval_randomize", False)):
            jitter_range = int(schedule.get("randomize_range_minutes", 0) or 0)
            if jitter_range > 0:
                interval_minutes += random.randint(-jitter_range, jitter_range)

        return max(1, interval_minutes)

    def get_effective_daily_limit(self, when: Optional[datetime] = None) -> int:
        """Return the active daily post limit for the current schedule."""
        schedule = self.get_effective_schedule(when)
        return max(0, int(schedule.get("daily_post_limit", 0) or 0))

    def is_weekly_schedule_paused(self, when: Optional[datetime] = None) -> bool:
        """Return True if the active weekly rule pauses posting."""
        return bool(self.get_effective_schedule(when).get("paused", False))

    def calculate_next_post_time(self, last_tick: Optional[str]) -> datetime:
        """
        Calculate when the next post should be created.

        Args:
            last_tick: ISO timestamp of last post tick

        Returns:
            Datetime of next post
        """
        if not last_tick:
            return datetime.now(timezone.utc)

        try:
            last = datetime.fromisoformat(last_tick)
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)

        interval_minutes = self.get_effective_interval_minutes()
        next_time = last + timedelta(minutes=interval_minutes)

        return next_time

    def should_create_next_post(self, last_tick: Optional[str]) -> bool:
        """
        Check if it's time to create the next post.

        Args:
            last_tick: ISO timestamp of last post tick

        Returns:
            True if time for next post
        """
        next_time = self.calculate_next_post_time(last_tick)
        now = datetime.now(timezone.utc)

        return now >= next_time

    def is_within_active_hours(self, when: Optional[datetime] = None) -> bool:
        """
        Check if current time is within active hours.

        Returns:
            True if within active hours (or if feature disabled)
        """
        schedule = self.get_effective_schedule(when)
        if schedule.get("paused"):
            return False

        local_now = self._local_now(when)
        if self._is_in_ranges(local_now, schedule.get("quiet_hours", [])):
            return False

        if not bool(schedule.get("active_hours_enabled", False)):
            return True

        # Parse start and end times
        start_time = parse_time(str(schedule.get("active_hours_start", "08:00")))
        end_time = parse_time(str(schedule.get("active_hours_end", "23:00")))

        if not start_time or not end_time:
            log("Invalid active hours format, ignoring", "WARN")
            return True

        current_hour = local_now.hour
        current_minute = local_now.minute
        current_total_minutes = current_hour * 60 + current_minute

        start_total = start_time[0] * 60 + start_time[1]
        end_total = end_time[0] * 60 + end_time[1]

        # Handle overnight ranges (e.g., 22:00 - 02:00)
        if start_total > end_total:
            # Active if after start OR before end
            return current_total_minutes >= start_total or current_total_minutes <= end_total
        else:
            # Active if between start and end
            return start_total <= current_total_minutes <= end_total

    def get_next_active_time(self) -> Optional[datetime]:
        """
        Get the next time when bot will be active.

        Returns:
            Datetime of next active period start, or None if always active
        """
        if not getattr(self.config, "active_hours_enabled", False) and not getattr(
            self.config, "weekly_schedule_enabled", False
        ):
            return None

        now = datetime.now(timezone.utc)
        if self.is_within_active_hours(now):
            return now

        # Search forward in five-minute steps. This keeps the logic simple while
        # supporting weekday/weekend overrides, quiet hours, and pause days.
        for minutes in range(5, (14 * 24 * 60) + 5, 5):
            candidate = now + timedelta(minutes=minutes)
            if self.is_within_active_hours(candidate):
                return candidate

        return None

    def should_rate_limit_previews(
        self, preview_times: List[str], max_previews: int = 8, window_minutes: int = 10
    ) -> bool:
        """
        Check if preview rate limit is exceeded.

        Args:
            preview_times: List of ISO timestamps of recent previews
            max_previews: Maximum previews allowed
            window_minutes: Time window in minutes

        Returns:
            True if rate limited
        """
        if not preview_times:
            return False

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=window_minutes)

        # Count recent previews
        recent = 0
        for time_str in preview_times:
            try:
                time_dt = datetime.fromisoformat(time_str)
                if time_dt >= cutoff:
                    recent += 1
            except (ValueError, TypeError):
                continue

        if recent >= max_previews:
            log(f"Rate limited: {recent} previews in last {window_minutes} minutes", "WARN")
            return True

        return False

    def clean_old_preview_times(self, preview_times: List[str], window_minutes: int = 10) -> List[str]:
        """
        Remove old preview timestamps outside the window.

        Args:
            preview_times: List of ISO timestamps
            window_minutes: Time window to keep

        Returns:
            Cleaned list
        """
        if not preview_times:
            return []

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=window_minutes)

        cleaned = []
        for time_str in preview_times:
            try:
                time_dt = datetime.fromisoformat(time_str)
                if time_dt >= cutoff:
                    cleaned.append(time_str)
            except (ValueError, TypeError):
                continue

        return cleaned

    def has_reached_daily_limit(self, daily_count: int) -> bool:
        """
        Check if daily post limit has been reached.

        Args:
            daily_count: Number of posts today

        Returns:
            True if limit reached
        """
        daily_limit = self.get_effective_daily_limit()
        if daily_limit <= 0:
            return False  # No limit

        return daily_count >= daily_limit

    def get_approval_deadline(self) -> datetime:
        """
        Get deadline for auto-approval.

        Returns:
            Datetime when pending item should auto-post
        """
        now = datetime.now(timezone.utc)
        return now + timedelta(minutes=self.config.auto_approve_after_minutes)

    def has_approval_expired(self, deadline: Optional[str]) -> bool:
        """
        Check if approval deadline has passed.

        Args:
            deadline: ISO timestamp of deadline

        Returns:
            True if deadline passed
        """
        if not deadline:
            return False

        try:
            deadline_dt = datetime.fromisoformat(deadline)
            now = datetime.now(timezone.utc)
            return now >= deadline_dt
        except (ValueError, TypeError):
            return False
