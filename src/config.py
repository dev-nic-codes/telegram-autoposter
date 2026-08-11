"""
Configuration management for Reddit to Telegram bot.
Handles loading, saving, and validating configuration.
"""

import copy
import json
import os
import re
import tempfile
from typing import List, Optional, Dict, Any


class Config:
    """Configuration manager"""

    SUBREDDIT_RULE_MEDIA_TYPES = {"any", "image", "video", "gallery"}
    WEEKLY_SCHEDULE_KEYS = {
        "weekday",
        "weekend",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
    WEEKLY_SCHEDULE_ALIASES = {
        "mon": "monday",
        "tue": "tuesday",
        "tues": "tuesday",
        "wed": "wednesday",
        "thu": "thursday",
        "thur": "thursday",
        "thurs": "thursday",
        "fri": "friday",
        "sat": "saturday",
        "sun": "sunday",
        "weekdays": "weekday",
        "weekends": "weekend",
    }
    SMART_SCORING_WEIGHT_DEFAULTS = {
        "upvotes": 0.35,
        "comments": 0.20,
        "freshness": 0.20,
        "media_type": 0.10,
        "title_quality": 0.15,
        "subreddit_repetition": 0.20,
    }
    EMERGENCY_PAUSE_CATEGORIES = ("reddit", "telegram", "download", "empty_feed")
    EMERGENCY_PAUSE_THRESHOLD_DEFAULTS = {
        "reddit": 5,
        "telegram": 3,
        "download": 6,
        "empty_feed": 4,
    }
    VIDEO_AUDIO_POLICIES = {"allow_silent", "prefer_audio", "require_audio"}
    VIDEO_ORIENTATION_RULES = {"any", "portrait", "landscape", "square"}

    CAPTION_MODES = {
        "template",
        "source",
        "source_plus_footer",
        "source_plus_body",
        "source_with_credit",
        "credit_only",
        "none",
        "variants",
    }

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.profile_name: str = "Default"
        self.profile_key: str = "default"
        self.state_file_override: Optional[str] = None
        self.bots: List[Dict[str, Any]] = []
        self._set_defaults()

    def _set_defaults(self) -> None:
        """Reset all configurable fields to their defaults."""
        # Telegram settings
        self.bot_token: str = ""
        self.admin_chat_id: int = 0
        self.admin_user_ids: List[int] = []
        self.channels: List[Dict[str, Any]] = []

        # Reddit settings
        self.subreddits: List[str] = []
        self.user_agent: str = "windows:telegram-autoposter:v2.0 (by /u/your_reddit_username)"
        self.reddit_client_id: str = ""
        self.reddit_client_secret: str = ""

        # Posting schedule
        self.post_interval_minutes: int = 45
        self.post_interval_randomize: bool = True
        self.randomize_range_minutes: int = 5

        # Active hours (24-hour format)
        self.active_hours_enabled: bool = False
        self.active_hours_start: str = "08:00"
        self.active_hours_end: str = "23:00"
        self.timezone: str = "UTC"
        self.weekly_schedule_enabled: bool = False
        self.weekly_schedule: Dict[str, Dict[str, Any]] = {}
        self.emergency_pause_enabled: bool = True
        self.emergency_pause_window_minutes: int = 30
        self.emergency_pause_thresholds: Dict[str, int] = dict(self.EMERGENCY_PAUSE_THRESHOLD_DEFAULTS)
        self.emergency_pause_notify_admin: bool = True
        self.auto_recovery_enabled: bool = True
        self.auto_recovery_upload_retries: int = 2
        self.auto_recovery_retry_delay_seconds: int = 5
        self.auto_recovery_compress_on_retry: bool = True
        self.auto_recovery_video_target_mb: int = 30
        self.auto_recovery_image_target_mb: int = 8
        self.auto_recovery_stuck_pending_minutes: int = 90
        self.auto_recovery_notify_threshold: int = 3
        self.auto_recovery_notify_window_minutes: int = 30
        self.auto_recovery_notify_cooldown_minutes: int = 30

        # Approval system
        self.auto_approve_after_minutes: int = 10
        self.approval_required: bool = True

        # Content filtering
        self.min_upvotes: int = 0
        self.max_post_age_hours: int = 24
        self.min_image_width: int = 800
        self.min_image_height: int = 0
        self.image_quality_rules_enabled: bool = True
        self.image_aspect_ratio_min: float = 0.20
        self.image_aspect_ratio_max: float = 5.00
        self.image_blur_filter_enabled: bool = False
        self.image_blur_score_min: float = 35.0
        self.image_screenshot_filter_enabled: bool = False
        self.image_text_heavy_filter_enabled: bool = False
        self.image_text_heavy_max_edge_density: float = 0.18
        self.skip_nsfw: bool = True
        self.title_blacklist: List[str] = ["onlyfans", "subscribe", "link in bio"]
        self.title_whitelist: List[str] = []
        self.subreddit_rules: Dict[str, Dict[str, Any]] = {}

        # Content variety
        self.max_images_in_row: int = 3
        self.avoid_duplicate_subreddit_streak: int = 3
        self.duplicate_crosspost_blocking: bool = True
        self.duplicate_title_similarity_enabled: bool = True
        self.duplicate_title_similarity_threshold: float = 0.88
        self.duplicate_title_similarity_history_limit: int = 500
        self.author_cooldown_enabled: bool = False
        self.author_cooldown_hours: int = 24
        self.smart_scoring_enabled: bool = True
        self.smart_scoring_top_pool_size: int = 8
        self.smart_scoring_weights: Dict[str, float] = dict(self.SMART_SCORING_WEIGHT_DEFAULTS)

        # Caption settings
        self.caption_mode: str = "template"
        self.caption_template: str = "@YourChannel"
        self.caption_footer_template: str = ""
        self.caption_variants: List[Dict[str, Any]] = []
        self.add_reddit_link_button: bool = False
        self.add_subreddit_hashtag: bool = False
        self.spoiler_posts_enabled: bool = False
        self.auto_reactions_enabled: bool = True
        self.auto_reaction_emojis: List[str] = [
            "\U0001f44d",
            "\u2764",
            "\U0001f525",
            "\U0001f970",
            "\U0001f44f",
            "\U0001f63b",
        ]

        # Media settings
        self.max_video_length_seconds: int = 300
        self.video_audio_policy: str = "allow_silent"
        self.video_orientation_rule: str = "any"
        self.video_convert_to_mp4: bool = True
        self.video_compression_enabled: bool = True
        self.video_compression_target_mb: int = 40
        self.max_download_mb: int = 45
        self.gallery_posts_enabled: bool = True
        self.min_gallery_items: int = 2
        self.max_gallery_items: int = 6
        self.domain_downloaders_enabled: bool = True
        self.imgur_album_downloads_enabled: bool = True
        self.html_media_resolver_enabled: bool = True

        # Performance
        self.reddit_cache_minutes: int = 3
        self.max_previews_per_10min: int = 8

        # Limits
        self.daily_post_limit: int = 32

    def _clean_list(self, raw_items: Any) -> List[str]:
        """Normalize a list of strings."""
        if not isinstance(raw_items, list):
            return []
        cleaned: List[str] = []
        for item in raw_items:
            value = str(item or "").strip()
            if value:
                cleaned.append(value)
        return cleaned

    def normalize_subreddit_name(self, subreddit: str) -> str:
        """Normalize a subreddit key for config lookups."""
        value = str(subreddit or "").strip().lower()
        for prefix in ("/r/", "r/"):
            if value.startswith(prefix):
                value = value[len(prefix) :].strip()
        return value

    def _coerce_int(self, value: Any, default: int = 0, *, minimum: int = 0) -> int:
        """Coerce a config value to a bounded integer."""
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        return max(minimum, result)

    def _coerce_float(
        self,
        value: Any,
        default: float = 1.0,
        *,
        minimum: float = 0.0,
        maximum: float = 100.0,
    ) -> float:
        """Coerce a config value to a bounded float."""
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
        return max(minimum, min(maximum, result))

    def _coerce_optional_bool(self, value: Any) -> Optional[bool]:
        """Coerce JSON/manual boolean values while preserving missing values."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        return None

    def _normalize_weekly_schedule_key(self, value: str) -> str:
        """Normalize a weekly schedule key."""
        key = str(value or "").strip().lower()
        key = self.WEEKLY_SCHEDULE_ALIASES.get(key, key)
        return key if key in self.WEEKLY_SCHEDULE_KEYS else ""

    def _clean_time_text(self, value: Any) -> str:
        """Normalize a HH:MM time string or return an empty string."""
        text = str(value or "").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not match:
            return ""
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return ""

    def _clean_time_range_list(
        self,
        raw_ranges: Any,
        *,
        allow_interval: bool = False,
    ) -> List[Dict[str, Any]]:
        """Normalize weekly schedule time ranges."""
        if isinstance(raw_ranges, dict):
            raw_items = [raw_ranges]
        elif isinstance(raw_ranges, list):
            raw_items = raw_ranges
        else:
            return []

        cleaned: List[Dict[str, Any]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            start = self._clean_time_text(raw_item.get("start"))
            end = self._clean_time_text(raw_item.get("end"))
            if not start or not end:
                continue

            item: Dict[str, Any] = {"start": start, "end": end}
            if allow_interval and "post_interval_minutes" in raw_item:
                item["post_interval_minutes"] = self._coerce_int(
                    raw_item.get("post_interval_minutes"),
                    self.post_interval_minutes,
                    minimum=1,
                )
            cleaned.append(item)

        return cleaned

    def _clean_weekly_schedule(self, raw_schedule: Any) -> Dict[str, Dict[str, Any]]:
        """Normalize weekly schedule overrides."""
        if not isinstance(raw_schedule, dict):
            return {}

        cleaned: Dict[str, Dict[str, Any]] = {}
        for raw_key, raw_rule in raw_schedule.items():
            key = self._normalize_weekly_schedule_key(str(raw_key or ""))
            if not key or not isinstance(raw_rule, dict):
                continue

            rule: Dict[str, Any] = {}

            if "paused" in raw_rule:
                paused = self._coerce_optional_bool(raw_rule.get("paused"))
                if paused is not None:
                    rule["paused"] = paused

            if "post_interval_minutes" in raw_rule:
                rule["post_interval_minutes"] = self._coerce_int(
                    raw_rule.get("post_interval_minutes"),
                    self.post_interval_minutes,
                    minimum=1,
                )

            if "post_interval_randomize" in raw_rule:
                randomized = self._coerce_optional_bool(raw_rule.get("post_interval_randomize"))
                if randomized is not None:
                    rule["post_interval_randomize"] = randomized

            if "randomize_range_minutes" in raw_rule:
                rule["randomize_range_minutes"] = self._coerce_int(
                    raw_rule.get("randomize_range_minutes"),
                    self.randomize_range_minutes,
                    minimum=0,
                )

            if "active_hours_enabled" in raw_rule:
                active = self._coerce_optional_bool(raw_rule.get("active_hours_enabled"))
                if active is not None:
                    rule["active_hours_enabled"] = active

            if "active_hours_start" in raw_rule:
                value = self._clean_time_text(raw_rule.get("active_hours_start"))
                if value:
                    rule["active_hours_start"] = value

            if "active_hours_end" in raw_rule:
                value = self._clean_time_text(raw_rule.get("active_hours_end"))
                if value:
                    rule["active_hours_end"] = value

            if "daily_post_limit" in raw_rule:
                rule["daily_post_limit"] = self._coerce_int(
                    raw_rule.get("daily_post_limit"),
                    self.daily_post_limit,
                    minimum=0,
                )

            quiet_hours = self._clean_time_range_list(raw_rule.get("quiet_hours"))
            if quiet_hours:
                rule["quiet_hours"] = quiet_hours

            peak_hours = self._clean_time_range_list(
                raw_rule.get("peak_hours"),
                allow_interval=True,
            )
            if peak_hours:
                rule["peak_hours"] = peak_hours

            if rule:
                cleaned[key] = rule

        return cleaned

    def get_weekly_schedule_rule(self, schedule_key: str) -> Dict[str, Any]:
        """Return one normalized weekly schedule rule."""
        key = self._normalize_weekly_schedule_key(schedule_key)
        if not key:
            return {}
        return dict(self.weekly_schedule.get(key, {}))

    def _normalize_rule_media_type(self, value: Any) -> str:
        """Normalize a per-subreddit media type rule."""
        media_type = str(value or "any").strip().lower()
        aliases = {
            "all": "any",
            "either": "any",
            "images": "image",
            "photo": "image",
            "photos": "image",
            "pics": "image",
            "videos": "video",
            "vid": "video",
            "album": "gallery",
            "albums": "gallery",
            "galleries": "gallery",
        }
        media_type = aliases.get(media_type, media_type)
        if media_type not in self.SUBREDDIT_RULE_MEDIA_TYPES:
            media_type = "any"
        return media_type

    def _clean_caption_variants(self, raw_variants: Any) -> List[Dict[str, Any]]:
        """Normalize caption variant rotation config."""
        if not isinstance(raw_variants, list):
            return []

        cleaned: List[Dict[str, Any]] = []
        for index, raw_variant in enumerate(raw_variants, 1):
            variant: Dict[str, Any] = {}

            if isinstance(raw_variant, str):
                template = raw_variant.strip()
                if not template:
                    continue
                variant = {
                    "name": f"Variant {index}",
                    "mode": "template",
                    "template": template,
                }
            elif isinstance(raw_variant, dict):
                name = str(raw_variant.get("name") or f"Variant {index}").strip()
                mode = str(raw_variant.get("mode") or "").strip().lower()
                template = str(raw_variant.get("template") or raw_variant.get("caption_template") or "").strip()
                footer_template = str(
                    raw_variant.get("footer_template")
                    or raw_variant.get("caption_footer_template")
                    or raw_variant.get("caption_footer")
                    or ""
                ).strip()

                if not mode:
                    mode = "template" if template else "source"
                if mode == "variants" or mode not in self.CAPTION_MODES:
                    mode = "template" if template else "source"

                if mode == "template" and not template:
                    continue
                if mode == "source_plus_footer" and not footer_template:
                    footer_template = self.caption_footer_template

                variant = {
                    "name": name or f"Variant {index}",
                    "mode": mode,
                }
                if template:
                    variant["template"] = template
                if footer_template:
                    variant["footer_template"] = footer_template
            else:
                continue

            cleaned.append(variant)

        return cleaned[:50]

    def _clean_subreddit_rules(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        """Normalize per-subreddit rule config."""
        if not isinstance(raw_rules, dict):
            return {}

        cleaned: Dict[str, Dict[str, Any]] = {}
        for raw_subreddit, raw_rule in raw_rules.items():
            subreddit = self.normalize_subreddit_name(str(raw_subreddit or ""))
            if not subreddit or not isinstance(raw_rule, dict):
                continue

            rule: Dict[str, Any] = {}

            if "min_upvotes" in raw_rule:
                rule["min_upvotes"] = self._coerce_int(raw_rule.get("min_upvotes"), 0)

            if "max_post_age_hours" in raw_rule:
                rule["max_post_age_hours"] = self._coerce_int(
                    raw_rule.get("max_post_age_hours"),
                    0,
                )

            if "media_type" in raw_rule:
                rule["media_type"] = self._normalize_rule_media_type(raw_rule.get("media_type"))

            if "skip_nsfw" in raw_rule:
                bool_value = self._coerce_optional_bool(raw_rule.get("skip_nsfw"))
                if bool_value is not None:
                    rule["skip_nsfw"] = bool_value

            if "caption_footer" in raw_rule:
                rule["caption_footer"] = str(raw_rule.get("caption_footer") or "").strip()

            if "caption_template" in raw_rule:
                rule["caption_template"] = str(raw_rule.get("caption_template") or "").strip()

            if "caption_variants" in raw_rule:
                variants = self._clean_caption_variants(raw_rule.get("caption_variants"))
                if variants:
                    rule["caption_variants"] = variants

            if "priority_weight" in raw_rule:
                rule["priority_weight"] = self._coerce_float(
                    raw_rule.get("priority_weight"),
                    1.0,
                )

            if rule:
                cleaned[subreddit] = rule

        return cleaned

    def _clean_emergency_pause_thresholds(self, raw_thresholds: Any) -> Dict[str, int]:
        """Normalize emergency pause thresholds by failure category."""
        thresholds = dict(self.EMERGENCY_PAUSE_THRESHOLD_DEFAULTS)
        if not isinstance(raw_thresholds, dict):
            return thresholds

        aliases = {
            "empty-feed": "empty_feed",
            "empty feed": "empty_feed",
            "empty": "empty_feed",
            "media": "download",
            "telegram_api": "telegram",
            "reddit_api": "reddit",
        }
        for raw_key, raw_value in raw_thresholds.items():
            key = aliases.get(str(raw_key or "").strip().lower(), str(raw_key or "").strip().lower())
            if key not in self.EMERGENCY_PAUSE_CATEGORIES:
                continue
            thresholds[key] = self._coerce_int(
                raw_value,
                thresholds[key],
                minimum=0,
            )

        return thresholds

    def _normalize_video_audio_policy(self, value: Any) -> str:
        """Normalize the video audio rule."""
        policy = str(value or "allow_silent").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "any": "allow_silent",
            "allow": "allow_silent",
            "silent_ok": "allow_silent",
            "optional": "allow_silent",
            "prefer": "prefer_audio",
            "preferred": "prefer_audio",
            "audio": "require_audio",
            "required": "require_audio",
            "require": "require_audio",
        }
        policy = aliases.get(policy, policy)
        if policy not in self.VIDEO_AUDIO_POLICIES:
            policy = "allow_silent"
        return policy

    def _normalize_video_orientation_rule(self, value: Any) -> str:
        """Normalize the video orientation rule."""
        rule = str(value or "any").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "all": "any",
            "vertical": "portrait",
            "horizontal": "landscape",
            "wide": "landscape",
            "1_1": "square",
        }
        rule = aliases.get(rule, rule)
        if rule not in self.VIDEO_ORIENTATION_RULES:
            rule = "any"
        return rule

    def _clean_smart_scoring_weights(self, raw_weights: Any) -> Dict[str, float]:
        """Normalize smart scoring component weights."""
        defaults = dict(self.SMART_SCORING_WEIGHT_DEFAULTS)
        if not isinstance(raw_weights, dict):
            return defaults

        cleaned = dict(defaults)
        for key in defaults:
            if key in raw_weights:
                cleaned[key] = self._coerce_float(
                    raw_weights.get(key),
                    defaults[key],
                    minimum=0.0,
                    maximum=10.0,
                )
        return cleaned

    def get_subreddit_rule(self, subreddit: str) -> Dict[str, Any]:
        """Return the configured rule overrides for a subreddit."""
        key = self.normalize_subreddit_name(subreddit)
        return dict(self.subreddit_rules.get(key, {}))

    def get_effective_subreddit_rule(self, subreddit: str) -> Dict[str, Any]:
        """Return global settings merged with a subreddit's overrides."""
        rule = self.get_subreddit_rule(subreddit)
        return {
            "min_upvotes": self._coerce_int(
                rule.get("min_upvotes", self.min_upvotes),
                self.min_upvotes,
            ),
            "max_post_age_hours": self._coerce_int(
                rule.get("max_post_age_hours", self.max_post_age_hours),
                self.max_post_age_hours,
            ),
            "media_type": self._normalize_rule_media_type(rule.get("media_type", "any")),
            "skip_nsfw": bool(rule.get("skip_nsfw", self.skip_nsfw)),
            "caption_footer": str(rule.get("caption_footer", "") or "").strip(),
            "caption_template": str(rule.get("caption_template", "") or "").strip(),
            "caption_variants": self._clean_caption_variants(rule.get("caption_variants", [])),
            "priority_weight": self._coerce_float(rule.get("priority_weight", 1.0), 1.0),
        }

    def get_subreddit_priority_weight(self, subreddit: str) -> float:
        """Return the weighted selection multiplier for a subreddit."""
        return float(self.get_effective_subreddit_rule(subreddit).get("priority_weight", 1.0))

    def get_subreddit_caption_footer(self, subreddit: str) -> str:
        """Return a per-subreddit caption footer template, if configured."""
        return str(self.get_effective_subreddit_rule(subreddit).get("caption_footer", "") or "")

    def get_subreddit_caption_template(self, subreddit: str) -> str:
        """Return a per-subreddit caption template, if configured."""
        return str(self.get_effective_subreddit_rule(subreddit).get("caption_template", "") or "")

    def get_subreddit_caption_variants(self, subreddit: str) -> List[Dict[str, Any]]:
        """Return per-subreddit caption variants, if configured."""
        return self._clean_caption_variants(self.get_effective_subreddit_rule(subreddit).get("caption_variants", []))

    def _sanitize_profile_key(self, raw_value: str, fallback: str) -> str:
        """Create a safe file/thread identifier for a bot profile."""
        value = (raw_value or "").strip().lower()
        if not value:
            value = fallback
        value = re.sub(r"[^a-z0-9._-]+", "-", value).strip("-._")
        return value or fallback

    def _apply_data(self, data: Dict[str, Any]) -> None:
        """Apply a config dictionary onto this instance."""
        self.profile_name = "Default"
        self.profile_key = "default"
        self.state_file_override = None
        self._set_defaults()

        self.bot_token = str(data.get("bot_token", "") or "").strip()

        try:
            self.admin_chat_id = int(data.get("admin_chat_id", 0) or 0)
        except (TypeError, ValueError):
            self.admin_chat_id = 0
        raw_admin_users = data.get("admin_user_ids", [])
        cleaned_admin_users: List[int] = []
        if isinstance(raw_admin_users, list):
            for item in raw_admin_users:
                try:
                    value = int(item or 0)
                except (TypeError, ValueError):
                    continue
                if value > 0 and value not in cleaned_admin_users:
                    cleaned_admin_users.append(value)
        self.admin_user_ids = cleaned_admin_users

        raw_channels = data.get("channels", [])
        if isinstance(raw_channels, list):
            self.channels = [copy.deepcopy(item) for item in raw_channels if isinstance(item, dict)]
        else:
            self.channels = []

        self.subreddits = self._clean_list(data.get("subreddits", []))
        self.user_agent = str(data.get("user_agent", self.user_agent) or self.user_agent).strip()
        self.reddit_client_id = str(data.get("reddit_client_id", "") or "").strip()
        self.reddit_client_secret = str(data.get("reddit_client_secret", "") or "").strip()

        self.post_interval_minutes = int(data.get("post_interval_minutes", 45) or 45)
        self.post_interval_randomize = bool(data.get("post_interval_randomize", True))
        self.randomize_range_minutes = int(data.get("randomize_range_minutes", 5) or 0)

        self.active_hours_enabled = bool(data.get("active_hours_enabled", False))
        self.active_hours_start = str(data.get("active_hours_start", "08:00") or "08:00")
        self.active_hours_end = str(data.get("active_hours_end", "23:00") or "23:00")
        self.timezone = str(data.get("timezone", "UTC") or "UTC")
        self.weekly_schedule_enabled = bool(data.get("weekly_schedule_enabled", False))
        self.weekly_schedule = self._clean_weekly_schedule(data.get("weekly_schedule", {}))
        self.emergency_pause_enabled = bool(data.get("emergency_pause_enabled", True))
        self.emergency_pause_window_minutes = self._coerce_int(
            data.get("emergency_pause_window_minutes", 30),
            30,
            minimum=1,
        )
        self.emergency_pause_thresholds = self._clean_emergency_pause_thresholds(
            data.get("emergency_pause_thresholds", {})
        )
        self.emergency_pause_notify_admin = bool(data.get("emergency_pause_notify_admin", True))
        self.auto_recovery_enabled = bool(data.get("auto_recovery_enabled", True))
        self.auto_recovery_upload_retries = self._coerce_int(
            data.get("auto_recovery_upload_retries", 2),
            2,
            minimum=0,
        )
        self.auto_recovery_upload_retries = min(5, self.auto_recovery_upload_retries)
        self.auto_recovery_retry_delay_seconds = self._coerce_int(
            data.get("auto_recovery_retry_delay_seconds", 5),
            5,
            minimum=0,
        )
        self.auto_recovery_retry_delay_seconds = min(60, self.auto_recovery_retry_delay_seconds)
        self.auto_recovery_compress_on_retry = bool(data.get("auto_recovery_compress_on_retry", True))
        self.auto_recovery_video_target_mb = min(
            45,
            self._coerce_int(data.get("auto_recovery_video_target_mb", 30), 30, minimum=1),
        )
        self.auto_recovery_image_target_mb = min(
            10,
            self._coerce_int(data.get("auto_recovery_image_target_mb", 8), 8, minimum=1),
        )
        self.auto_recovery_stuck_pending_minutes = self._coerce_int(
            data.get("auto_recovery_stuck_pending_minutes", 90),
            90,
            minimum=1,
        )
        self.auto_recovery_notify_threshold = self._coerce_int(
            data.get("auto_recovery_notify_threshold", 3),
            3,
            minimum=1,
        )
        self.auto_recovery_notify_window_minutes = self._coerce_int(
            data.get("auto_recovery_notify_window_minutes", 30),
            30,
            minimum=1,
        )
        self.auto_recovery_notify_cooldown_minutes = self._coerce_int(
            data.get("auto_recovery_notify_cooldown_minutes", 30),
            30,
            minimum=1,
        )

        self.auto_approve_after_minutes = int(data.get("auto_approve_after_minutes", 10) or 0)
        self.approval_required = bool(data.get("approval_required", True))

        self.min_upvotes = int(data.get("min_upvotes", 0) or 0)
        self.max_post_age_hours = int(data.get("max_post_age_hours", 24) or 0)
        self.min_image_width = self._coerce_int(data.get("min_image_width", 800), 800, minimum=0)
        self.min_image_height = self._coerce_int(data.get("min_image_height", 0), 0, minimum=0)
        self.image_quality_rules_enabled = bool(data.get("image_quality_rules_enabled", True))
        self.image_aspect_ratio_min = self._coerce_float(
            data.get("image_aspect_ratio_min", 0.20),
            0.20,
            minimum=0.05,
            maximum=20.0,
        )
        self.image_aspect_ratio_max = self._coerce_float(
            data.get("image_aspect_ratio_max", 5.00),
            5.00,
            minimum=0.05,
            maximum=20.0,
        )
        if self.image_aspect_ratio_max < self.image_aspect_ratio_min:
            self.image_aspect_ratio_min, self.image_aspect_ratio_max = (
                self.image_aspect_ratio_max,
                self.image_aspect_ratio_min,
            )
        self.image_blur_filter_enabled = bool(data.get("image_blur_filter_enabled", False))
        self.image_blur_score_min = self._coerce_float(
            data.get("image_blur_score_min", 35.0),
            35.0,
            minimum=0.0,
            maximum=10000.0,
        )
        self.image_screenshot_filter_enabled = bool(data.get("image_screenshot_filter_enabled", False))
        self.image_text_heavy_filter_enabled = bool(data.get("image_text_heavy_filter_enabled", False))
        self.image_text_heavy_max_edge_density = self._coerce_float(
            data.get("image_text_heavy_max_edge_density", 0.18),
            0.18,
            minimum=0.01,
            maximum=1.0,
        )
        self.skip_nsfw = bool(data.get("skip_nsfw", True))
        self.title_blacklist = self._clean_list(data.get("title_blacklist", []))
        self.title_whitelist = self._clean_list(data.get("title_whitelist", []))
        self.subreddit_rules = self._clean_subreddit_rules(data.get("subreddit_rules", {}))

        self.max_images_in_row = int(data.get("max_images_in_row", 3) or 0)
        self.avoid_duplicate_subreddit_streak = int(data.get("avoid_duplicate_subreddit_streak", 3) or 0)
        self.duplicate_crosspost_blocking = bool(data.get("duplicate_crosspost_blocking", True))
        self.duplicate_title_similarity_enabled = bool(data.get("duplicate_title_similarity_enabled", True))
        self.duplicate_title_similarity_threshold = self._coerce_float(
            data.get("duplicate_title_similarity_threshold", 0.88),
            0.88,
            minimum=0.5,
            maximum=1.0,
        )
        self.duplicate_title_similarity_history_limit = self._coerce_int(
            data.get("duplicate_title_similarity_history_limit", 500),
            500,
            minimum=1,
        )
        self.author_cooldown_enabled = bool(data.get("author_cooldown_enabled", False))
        self.author_cooldown_hours = self._coerce_int(
            data.get("author_cooldown_hours", 24),
            24,
            minimum=0,
        )
        self.smart_scoring_enabled = bool(data.get("smart_scoring_enabled", True))
        self.smart_scoring_top_pool_size = self._coerce_int(
            data.get("smart_scoring_top_pool_size", 8),
            8,
            minimum=1,
        )
        self.smart_scoring_weights = self._clean_smart_scoring_weights(data.get("smart_scoring_weights", {}))

        self.caption_mode = str(data.get("caption_mode", "template") or "template").strip()
        if self.caption_mode not in self.CAPTION_MODES:
            self.caption_mode = "template"
        self.caption_template = str(data.get("caption_template", "@YourChannel") or "")
        self.caption_footer_template = str(data.get("caption_footer_template", "") or "")
        self.caption_variants = self._clean_caption_variants(data.get("caption_variants", []))
        self.add_reddit_link_button = bool(data.get("add_reddit_link_button", False))
        self.add_subreddit_hashtag = bool(data.get("add_subreddit_hashtag", False))
        self.spoiler_posts_enabled = bool(data.get("spoiler_posts_enabled", False))
        self.auto_reactions_enabled = bool(data.get("auto_reactions_enabled", True))
        self.auto_reaction_emojis = self._clean_list(
            data.get(
                "auto_reaction_emojis",
                [
                    "\U0001f44d",
                    "\u2764",
                    "\U0001f525",
                    "\U0001f970",
                    "\U0001f44f",
                    "\U0001f63b",
                ],
            )
        )

        self.max_video_length_seconds = self._coerce_int(
            data.get("max_video_length_seconds", 300),
            300,
            minimum=0,
        )
        self.video_audio_policy = self._normalize_video_audio_policy(data.get("video_audio_policy", "allow_silent"))
        self.video_orientation_rule = self._normalize_video_orientation_rule(data.get("video_orientation_rule", "any"))
        self.video_convert_to_mp4 = bool(data.get("video_convert_to_mp4", True))
        self.video_compression_enabled = bool(data.get("video_compression_enabled", True))
        self.max_download_mb = int(data.get("max_download_mb", 45) or 0)
        self.video_compression_target_mb = min(
            max(1, self.max_download_mb or 45),
            self._coerce_int(data.get("video_compression_target_mb", 40), 40, minimum=1),
        )
        self.auto_recovery_video_target_mb = min(
            max(1, self.max_download_mb or 45),
            self.auto_recovery_video_target_mb,
        )
        self.gallery_posts_enabled = bool(data.get("gallery_posts_enabled", True))
        self.max_gallery_items = min(
            10,
            self._coerce_int(data.get("max_gallery_items", 6), 6, minimum=2),
        )
        self.min_gallery_items = min(
            self.max_gallery_items,
            self._coerce_int(data.get("min_gallery_items", 2), 2, minimum=2),
        )
        self.domain_downloaders_enabled = bool(data.get("domain_downloaders_enabled", True))
        self.imgur_album_downloads_enabled = bool(data.get("imgur_album_downloads_enabled", True))
        self.html_media_resolver_enabled = bool(data.get("html_media_resolver_enabled", True))

        self.reddit_cache_minutes = int(data.get("reddit_cache_minutes", 3) or 0)
        self.max_previews_per_10min = int(data.get("max_previews_per_10min", 8) or 0)

        self.daily_post_limit = int(data.get("daily_post_limit", 32) or 0)

        raw_name = str(data.get("name", "") or data.get("profile_name", "") or "").strip()
        if raw_name:
            self.profile_name = raw_name

        raw_key = str(data.get("id", "") or "").strip()
        if raw_key:
            self.profile_key = self._sanitize_profile_key(raw_key, self.profile_key)
        elif raw_name:
            self.profile_key = self._sanitize_profile_key(raw_name, self.profile_key)

        raw_state_file = data.get("state_file")
        if isinstance(raw_state_file, str) and raw_state_file.strip():
            self.state_file_override = raw_state_file.strip()
        else:
            self.state_file_override = None

    def _to_dict(self, *, include_bots: bool = True) -> Dict[str, Any]:
        """Serialize the current config back to JSON-compatible data."""
        data = {
            "bot_token": self.bot_token,
            "admin_chat_id": self.admin_chat_id,
            "admin_user_ids": list(self.admin_user_ids),
            "channels": copy.deepcopy(self.channels),
            "subreddits": list(self.subreddits),
            "user_agent": self.user_agent,
            "reddit_client_id": self.reddit_client_id,
            "reddit_client_secret": self.reddit_client_secret,
            "post_interval_minutes": self.post_interval_minutes,
            "post_interval_randomize": self.post_interval_randomize,
            "randomize_range_minutes": self.randomize_range_minutes,
            "active_hours_enabled": self.active_hours_enabled,
            "active_hours_start": self.active_hours_start,
            "active_hours_end": self.active_hours_end,
            "timezone": self.timezone,
            "weekly_schedule_enabled": self.weekly_schedule_enabled,
            "weekly_schedule": copy.deepcopy(self.weekly_schedule),
            "emergency_pause_enabled": self.emergency_pause_enabled,
            "emergency_pause_window_minutes": self.emergency_pause_window_minutes,
            "emergency_pause_thresholds": dict(self.emergency_pause_thresholds),
            "emergency_pause_notify_admin": self.emergency_pause_notify_admin,
            "auto_recovery_enabled": self.auto_recovery_enabled,
            "auto_recovery_upload_retries": self.auto_recovery_upload_retries,
            "auto_recovery_retry_delay_seconds": self.auto_recovery_retry_delay_seconds,
            "auto_recovery_compress_on_retry": self.auto_recovery_compress_on_retry,
            "auto_recovery_video_target_mb": self.auto_recovery_video_target_mb,
            "auto_recovery_image_target_mb": self.auto_recovery_image_target_mb,
            "auto_recovery_stuck_pending_minutes": self.auto_recovery_stuck_pending_minutes,
            "auto_recovery_notify_threshold": self.auto_recovery_notify_threshold,
            "auto_recovery_notify_window_minutes": self.auto_recovery_notify_window_minutes,
            "auto_recovery_notify_cooldown_minutes": self.auto_recovery_notify_cooldown_minutes,
            "auto_approve_after_minutes": self.auto_approve_after_minutes,
            "approval_required": self.approval_required,
            "min_upvotes": self.min_upvotes,
            "max_post_age_hours": self.max_post_age_hours,
            "min_image_width": self.min_image_width,
            "min_image_height": self.min_image_height,
            "image_quality_rules_enabled": self.image_quality_rules_enabled,
            "image_aspect_ratio_min": self.image_aspect_ratio_min,
            "image_aspect_ratio_max": self.image_aspect_ratio_max,
            "image_blur_filter_enabled": self.image_blur_filter_enabled,
            "image_blur_score_min": self.image_blur_score_min,
            "image_screenshot_filter_enabled": self.image_screenshot_filter_enabled,
            "image_text_heavy_filter_enabled": self.image_text_heavy_filter_enabled,
            "image_text_heavy_max_edge_density": self.image_text_heavy_max_edge_density,
            "skip_nsfw": self.skip_nsfw,
            "title_blacklist": list(self.title_blacklist),
            "title_whitelist": list(self.title_whitelist),
            "subreddit_rules": copy.deepcopy(self.subreddit_rules),
            "max_images_in_row": self.max_images_in_row,
            "avoid_duplicate_subreddit_streak": self.avoid_duplicate_subreddit_streak,
            "duplicate_crosspost_blocking": self.duplicate_crosspost_blocking,
            "duplicate_title_similarity_enabled": self.duplicate_title_similarity_enabled,
            "duplicate_title_similarity_threshold": self.duplicate_title_similarity_threshold,
            "duplicate_title_similarity_history_limit": self.duplicate_title_similarity_history_limit,
            "author_cooldown_enabled": self.author_cooldown_enabled,
            "author_cooldown_hours": self.author_cooldown_hours,
            "smart_scoring_enabled": self.smart_scoring_enabled,
            "smart_scoring_top_pool_size": self.smart_scoring_top_pool_size,
            "smart_scoring_weights": dict(self.smart_scoring_weights),
            "caption_mode": self.caption_mode,
            "caption_template": self.caption_template,
            "caption_footer_template": self.caption_footer_template,
            "caption_variants": copy.deepcopy(self.caption_variants),
            "add_reddit_link_button": self.add_reddit_link_button,
            "add_subreddit_hashtag": self.add_subreddit_hashtag,
            "spoiler_posts_enabled": self.spoiler_posts_enabled,
            "auto_reactions_enabled": self.auto_reactions_enabled,
            "auto_reaction_emojis": list(self.auto_reaction_emojis),
            "max_video_length_seconds": self.max_video_length_seconds,
            "video_audio_policy": self.video_audio_policy,
            "video_orientation_rule": self.video_orientation_rule,
            "video_convert_to_mp4": self.video_convert_to_mp4,
            "video_compression_enabled": self.video_compression_enabled,
            "video_compression_target_mb": self.video_compression_target_mb,
            "max_download_mb": self.max_download_mb,
            "gallery_posts_enabled": self.gallery_posts_enabled,
            "min_gallery_items": self.min_gallery_items,
            "max_gallery_items": self.max_gallery_items,
            "domain_downloaders_enabled": self.domain_downloaders_enabled,
            "imgur_album_downloads_enabled": self.imgur_album_downloads_enabled,
            "html_media_resolver_enabled": self.html_media_resolver_enabled,
            "reddit_cache_minutes": self.reddit_cache_minutes,
            "max_previews_per_10min": self.max_previews_per_10min,
            "daily_post_limit": self.daily_post_limit,
        }

        if include_bots and self.bots:
            data["bots"] = copy.deepcopy(self.bots)

        return data

    def load(self) -> bool:
        """Load configuration from file."""
        if not os.path.exists(self.config_path):
            return False

        try:
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                raise ValueError("Config file must contain a JSON object")

            self._apply_data(data)
            raw_bots = data.get("bots", [])
            if isinstance(raw_bots, list):
                self.bots = [copy.deepcopy(item) for item in raw_bots if isinstance(item, dict)]
            else:
                self.bots = []
            return True
        except Exception as e:
            print(f"Error loading config: {e}")
            return False

    def save(self) -> bool:
        """Save configuration to file."""
        temp_path: Optional[str] = None
        try:
            parent = os.path.dirname(os.path.abspath(self.config_path))
            if parent:
                os.makedirs(parent, exist_ok=True)

            data = self._to_dict(include_bots=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(self.config_path)}.",
                suffix=".tmp",
                dir=parent or None,
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, self.config_path)
            temp_path = None
            try:
                os.chmod(self.config_path, 0o600)
            except OSError:
                pass

            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except FileNotFoundError:
                    pass

    def is_multi_bot_config(self) -> bool:
        """Return True when the config defines multiple bot profiles."""
        return len(self.bots) > 0

    def has_reddit_oauth_credentials(self) -> bool:
        """Return True when both Reddit OAuth credentials are configured."""
        return bool(self.reddit_client_id and self.reddit_client_secret)

    def has_partial_reddit_oauth_credentials(self) -> bool:
        """Return True when only one Reddit OAuth credential is configured."""
        return bool(self.reddit_client_id) != bool(self.reddit_client_secret)

    def uses_legacy_reddit_user_agent(self) -> bool:
        """Return True when the configured Reddit user-agent is blank, legacy, or placeholder."""
        value = str(self.user_agent or "").strip().lower()
        if not value:
            return True
        return value.startswith("mozilla/5.0") or "reddittotelegram/2.0" in value or "your_reddit_username" in value

    def is_configured(self) -> bool:
        """Check if essential configuration is present."""
        if self.is_multi_bot_config():
            valid, _ = self.validate()
            return valid

        return bool(self.bot_token and self.get_admin_user_ids() and self.channels and self.subreddits)

    def validate(self) -> tuple[bool, str]:
        """Validate configuration."""
        if self.is_multi_bot_config():
            seen_keys = set()
            runtime_configs = self.build_runtime_configs()
            if not runtime_configs:
                return False, "No bot profiles configured"

            for index, bot_cfg in enumerate(runtime_configs, 1):
                if bot_cfg.profile_key in seen_keys:
                    return False, f"Duplicate bot profile key: {bot_cfg.profile_key}"
                seen_keys.add(bot_cfg.profile_key)

                valid, error = bot_cfg.validate()
                if not valid:
                    return False, f"Bot {index} ({bot_cfg.profile_name}): {error}"

            return True, "Configuration is valid"

        if not self.bot_token:
            return False, "Bot token is missing"

        if not self.get_admin_user_ids():
            return False, "Admin user IDs are missing"

        if not self.channels:
            return False, "No channels configured"

        if not self.subreddits:
            return False, "No subreddits configured"

        if self.has_partial_reddit_oauth_credentials():
            return False, "Reddit OAuth requires both reddit_client_id and reddit_client_secret"

        if not str(self.user_agent or "").strip():
            return False, "Reddit user_agent is missing"

        if self.has_reddit_oauth_credentials() and self.uses_legacy_reddit_user_agent():
            return (
                False,
                "Reddit user_agent must be unique and descriptive, not a browser or placeholder string",
            )

        if self.post_interval_minutes < 1:
            return False, "Post interval must be at least 1 minute"

        if self.max_download_mb > 50:
            return False, "Max download size cannot exceed 50 MB (Telegram limit)"

        return True, "Configuration is valid"

    def get_default_channel(self) -> Optional[str]:
        """Get the first channel username."""
        if self.channels:
            return self.channels[0].get("username", "")
        return None

    def get_admin_user_ids(self) -> List[int]:
        """Return all authorized admin user IDs in stable order."""
        values: List[int] = []
        for item in [self.admin_chat_id, *self.admin_user_ids]:
            try:
                value = int(item or 0)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in values:
                values.append(value)
        return values

    def is_admin_user(self, user_id: Any) -> bool:
        """Return True when the given Telegram user/chat id is an authorized admin."""
        try:
            value = int(user_id or 0)
        except (TypeError, ValueError):
            return False
        return value in self.get_admin_user_ids()

    def set_default_channel(self, username: str) -> None:
        """Set the primary destination channel username."""
        value = str(username or "").strip()
        if value and not value.startswith("@"):
            value = "@" + value
        if not value:
            self.channels = []
            return
        entry = {"username": value, "description": "Main channel"}
        if self.channels:
            self.channels[0] = entry
        else:
            self.channels = [entry]

    def build_runtime_configs(self) -> List["Config"]:
        """Build concrete per-bot configs for runtime use."""
        if not self.is_multi_bot_config():
            cfg = Config(self.config_path)
            cfg._apply_data(self._to_dict(include_bots=False))
            cfg.profile_name = self.profile_name or "Default"
            cfg.profile_key = self.profile_key or "default"
            cfg.state_file_override = self.state_file_override
            cfg.bots = []
            return [cfg]

        base_data = self._to_dict(include_bots=False)
        runtime_configs: List[Config] = []

        for index, bot_data in enumerate(self.bots, 1):
            merged = copy.deepcopy(base_data)
            for key, value in bot_data.items():
                if key == "bots":
                    continue
                merged[key] = copy.deepcopy(value)

            cfg = Config(self.config_path)
            cfg._apply_data(merged)
            cfg.profile_name = (
                str(bot_data.get("name") or bot_data.get("id") or f"Bot {index}").strip() or f"Bot {index}"
            )
            cfg.profile_key = self._sanitize_profile_key(
                str(bot_data.get("id") or cfg.profile_name),
                f"bot_{index}",
            )

            raw_state_file = bot_data.get("state_file")
            if isinstance(raw_state_file, str) and raw_state_file.strip():
                cfg.state_file_override = raw_state_file.strip()
            else:
                cfg.state_file_override = None

            cfg.bots = []
            runtime_configs.append(cfg)

        return runtime_configs

    def create_template(self) -> None:
        """Create a template configuration file."""
        self.profile_name = "Default"
        self.profile_key = "default"
        self.state_file_override = None
        self.bots = []
        self.bot_token = "YOUR_BOT_TOKEN_HERE"
        self.admin_chat_id = 0
        self.admin_user_ids = []
        self.channels = [
            {
                "username": "@YourChannel",
                "description": "Main channel",
            }
        ]
        self.subreddits = [
            "supermodelcats",
            "sillycats",
            "catsoncats",
            "CatsBeingAdorable",
            "cutecats",
            "catsareliquid",
            "funnycats",
            "CatsWithDogs",
        ]
        self.caption_mode = "template"
        self.save()
