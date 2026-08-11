"""
State management for persistent bot data.
Handles saving and loading bot state to/from JSON file.
"""

import json
import os
import re
import html
import threading
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Set
from urllib.parse import unquote, urlsplit
from utils import log
from url_utils import path_for_domain


class StateManager:
    """Manages persistent state for the bot"""

    ERROR_HISTORY_LIMIT = 200
    RECOVERY_HISTORY_LIMIT = 500

    def __init__(self, state_file: str = "state.json"):
        self.state_file = state_file
        self.state: Dict[str, Any] = self._default_state()
        self._io_lock = threading.RLock()

    def _ensure_parent_dir(self) -> None:
        """Create the state file parent directory when needed."""
        parent = os.path.dirname(os.path.abspath(self.state_file))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _default_state(self) -> Dict[str, Any]:
        """Return default state structure"""
        return {
            "seen_posts": [],  # Attempted Reddit post IDs, kept for diagnostics/history
            "seen_media_urls": [],  # Attempted media URLs, kept for diagnostics/history
            "seen_signatures": [],  # Attempted content fingerprints, kept for diagnostics/history
            "blocked_signatures": [],  # Permanent fingerprints for posted/skipped content
            "posted_posts": [],  # List of successfully posted Reddit IDs
            "skipped_posts": [],  # List of skipped Reddit IDs
            "posted_media_urls": [],  # Media URLs successfully posted
            "skipped_media_urls": [],  # Media URLs skipped by admin
            "img_streak": 0,  # Current image streak count
            "subreddit_streak": [],  # Recent subreddit names
            "pending": None,  # Current pending approval item
            "post_queue": [],  # Approved items waiting for scheduled posting
            "blocked_authors": [],  # Authors blocked from admin preview buttons
            "blocked_subreddits": [],  # Subreddits blocked from admin preview buttons
            "blocked_title_keywords": [],  # Title keywords blocked from admin preview buttons
            "author_history": [],  # Recently posted authors for cooldown checks
            "tg_update_offset": 0,  # Telegram update offset
            "last_tick_utc": None,  # Last posting tick timestamp
            "reddit_backoff_until_utc": None,  # Reddit backoff deadline
            "preview_times_utc": [],  # Recent preview timestamps
            "cache": {},  # Reddit subreddit cache
            "post_history": [],  # History of posted items
            "post_analytics": [],  # Per-post performance tracking
            "daily_posts_count": 0,  # Posts today
            "daily_skips_count": 0,  # Skips today
            "daily_failures_count": 0,  # Failures today
            "daily_posts_date": None,  # Date for daily counter
            "last_error": None,  # Last recorded runtime error
            "last_error_utc": None,  # Timestamp of last runtime error
            "error_history": [],  # Recent runtime and emergency failures
            "recovery_events": [],  # Recent auto-recovery actions and failures
            "recovery_last_notice_utc": None,  # Last admin notice for recovery failures
            "emergency_failures": [],  # Rolling failure events for emergency pause rules
            "emergency_pause": {
                "active": False,
                "category": None,
                "reason": None,
                "triggered_at_utc": None,
                "count": 0,
                "threshold": 0,
                "window_minutes": 0,
            },
            "paused": False,  # Pause state
            "stats": {
                "total_posts": 0,
                "total_approvals": 0,
                "total_skips": 0,
                "start_date": datetime.now(timezone.utc).isoformat(),
            },
        }

    def load(self, quiet: bool = False) -> bool:
        """Load state from file"""
        if not os.path.exists(self.state_file):
            if not quiet:
                log(f"No state file found, creating new: {self.state_file}")
            self.state = self._default_state()
            return False

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # Merge with defaults to ensure all keys exist
            default = self._default_state()
            for key, value in default.items():
                if key not in loaded:
                    loaded[key] = value

            self.state = loaded

            # Ensure expected list types exist even if state was edited manually
            if not isinstance(self.state.get("seen_posts"), list):
                self.state["seen_posts"] = []
            if not isinstance(self.state.get("seen_media_urls"), list):
                self.state["seen_media_urls"] = []
            if not isinstance(self.state.get("seen_signatures"), list):
                self.state["seen_signatures"] = []
            if not isinstance(self.state.get("blocked_signatures"), list):
                self.state["blocked_signatures"] = []
            if not isinstance(self.state.get("posted_posts"), list):
                self.state["posted_posts"] = []
            if not isinstance(self.state.get("skipped_posts"), list):
                self.state["skipped_posts"] = []
            if not isinstance(self.state.get("posted_media_urls"), list):
                self.state["posted_media_urls"] = []
            if not isinstance(self.state.get("skipped_media_urls"), list):
                self.state["skipped_media_urls"] = []
            if not isinstance(self.state.get("post_queue"), list):
                self.state["post_queue"] = []
            if not isinstance(self.state.get("post_analytics"), list):
                self.state["post_analytics"] = []
            if not isinstance(self.state.get("blocked_authors"), list):
                self.state["blocked_authors"] = []
            if not isinstance(self.state.get("blocked_subreddits"), list):
                self.state["blocked_subreddits"] = []
            if not isinstance(self.state.get("blocked_title_keywords"), list):
                self.state["blocked_title_keywords"] = []
            if not isinstance(self.state.get("author_history"), list):
                self.state["author_history"] = []
            if not isinstance(self.state.get("error_history"), list):
                self.state["error_history"] = []
            if not isinstance(self.state.get("recovery_events"), list):
                self.state["recovery_events"] = []
            if not isinstance(self.state.get("emergency_failures"), list):
                self.state["emergency_failures"] = []
            if not isinstance(self.state.get("emergency_pause"), dict):
                self.state["emergency_pause"] = dict(default["emergency_pause"])
            for key in ("daily_posts_count", "daily_skips_count", "daily_failures_count"):
                try:
                    self.state[key] = max(0, int(self.state.get(key, 0) or 0))
                except (TypeError, ValueError):
                    self.state[key] = 0

            # Trim seen posts to prevent file bloat
            if len(self.state["seen_posts"]) > 10000:
                self.state["seen_posts"] = self.state["seen_posts"][-5000:]
            if len(self.state["seen_media_urls"]) > 10000:
                self.state["seen_media_urls"] = self.state["seen_media_urls"][-5000:]
            if len(self.state["seen_signatures"]) > 10000:
                self.state["seen_signatures"] = self.state["seen_signatures"][-5000:]
            if len(self.state["blocked_signatures"]) > 10000:
                self.state["blocked_signatures"] = self.state["blocked_signatures"][-5000:]
            if len(self.state["posted_posts"]) > 10000:
                self.state["posted_posts"] = self.state["posted_posts"][-5000:]
            if len(self.state["skipped_posts"]) > 10000:
                self.state["skipped_posts"] = self.state["skipped_posts"][-5000:]
            if len(self.state["posted_media_urls"]) > 10000:
                self.state["posted_media_urls"] = self.state["posted_media_urls"][-5000:]
            if len(self.state["skipped_media_urls"]) > 10000:
                self.state["skipped_media_urls"] = self.state["skipped_media_urls"][-5000:]
            if len(self.state["post_queue"]) > 100:
                self.state["post_queue"] = self.state["post_queue"][:100]
            if len(self.state["post_analytics"]) > 4000:
                self.state["post_analytics"] = self.state["post_analytics"][-2000:]
            if len(self.state["author_history"]) > 5000:
                self.state["author_history"] = self.state["author_history"][-2000:]
            self._trim_error_history()
            self._trim_recovery_events()
            if len(self.state["emergency_failures"]) > 1000:
                self.state["emergency_failures"] = self.state["emergency_failures"][-500:]
            self._clean_block_values("blocked_authors")
            self._clean_block_values("blocked_subreddits")
            self._clean_block_values("blocked_title_keywords")

            if not quiet:
                log(f"State loaded: {len(self.state['seen_posts'])} seen posts, pending={self.has_pending()}")
            return True

        except Exception as e:
            if not quiet:
                log(f"Error loading state: {e}", "ERROR")
            self.state = self._default_state()
            return False

    def save(self) -> bool:
        """Save state to file"""
        temp_file = f"{self.state_file}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with self._io_lock:
                self._ensure_parent_dir()

                # Trim data before saving
                self.state["seen_posts"] = list(self.state["seen_posts"])[-5000:]
                self.state["seen_media_urls"] = list(self.state.get("seen_media_urls", []))[-5000:]
                self.state["seen_signatures"] = list(self.state.get("seen_signatures", []))[-5000:]
                self.state["blocked_signatures"] = list(self.state.get("blocked_signatures", []))[-5000:]
                self.state["posted_posts"] = list(self.state.get("posted_posts", []))[-5000:]
                self.state["skipped_posts"] = list(self.state.get("skipped_posts", []))[-5000:]
                self.state["posted_media_urls"] = list(self.state.get("posted_media_urls", []))[-5000:]
                self.state["skipped_media_urls"] = list(self.state.get("skipped_media_urls", []))[-5000:]
                self.state["preview_times_utc"] = list(self.state["preview_times_utc"])[-50:]
                self.state["subreddit_streak"] = list(self.state["subreddit_streak"])[-10:]
                self.state["post_history"] = list(self.state["post_history"])[-100:]
                self.state["post_analytics"] = [
                    item for item in list(self.state.get("post_analytics", [])) if isinstance(item, dict)
                ][-2000:]
                self.state["author_history"] = [
                    item for item in list(self.state.get("author_history", [])) if isinstance(item, dict)
                ][-2000:]
                self.state["post_queue"] = [
                    item for item in list(self.state.get("post_queue", [])) if isinstance(item, dict)
                ][:100]
                self._trim_error_history()
                self._trim_recovery_events()
                self.state["emergency_failures"] = [
                    item for item in list(self.state.get("emergency_failures", [])) if isinstance(item, dict)
                ][-500:]
                if not isinstance(self.state.get("emergency_pause"), dict):
                    self.state["emergency_pause"] = dict(self._default_state()["emergency_pause"])
                self._clean_block_values("blocked_authors")
                self._clean_block_values("blocked_subreddits")
                self._clean_block_values("blocked_title_keywords")

                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.chmod(temp_file, 0o600)
                except OSError:
                    pass
                os.replace(temp_file, self.state_file)
                try:
                    os.chmod(self.state_file, 0o600)
                except OSError:
                    pass

            return True
        except Exception as e:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass
            log(f"Error saving state: {e}", "ERROR")
            return False

    def backup(self) -> bool:
        """Create a backup of the state file"""
        if not os.path.exists(self.state_file):
            return False

        try:
            self._ensure_parent_dir()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = f"{self.state_file}.backup_{timestamp}"

            with open(self.state_file, "r", encoding="utf-8") as f:
                data = f.read()

            with open(backup_file, "w", encoding="utf-8") as f:
                f.write(data)
            try:
                os.chmod(backup_file, 0o600)
            except OSError:
                pass

            log(f"State backed up to: {backup_file}")
            return True
        except Exception as e:
            log(f"Error backing up state: {e}", "ERROR")
            return False

    # Seen posts
    def get_seen_posts(self) -> Set[str]:
        """Get set of seen post IDs"""
        return set(self.state["seen_posts"])

    def mark_seen(
        self,
        post_id: str,
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
        post: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark a post as seen"""
        if post_id not in self.state["seen_posts"]:
            self.state["seen_posts"].append(post_id)
        for media_url in self._post_media_urls(post, url):
            self.mark_seen_media(media_url)
        self.mark_seen_signatures(
            post=post,
            post_id=post_id,
            url=url,
            title=title,
            permalink=permalink,
            crosspost_parent=crosspost_parent,
        )

    def is_seen(self, post_id: str) -> bool:
        """Check if post has been seen"""
        return post_id in self.state["seen_posts"]

    # Seen media URLs
    def _normalize_url(self, url: str) -> str:
        """
        Normalize media URL for deduping across redirects and transient params.
        The scheme is intentionally omitted so http/https variants match.
        """
        if not url:
            return ""
        value = html.unescape(str(url or "")).strip()
        if not value:
            return ""

        try:
            parsed = urlsplit(value)
        except ValueError:
            return value.split("?", 1)[0].split("#", 1)[0].strip().rstrip("/")

        if not parsed.netloc:
            return value.split("?", 1)[0].split("#", 1)[0].strip().rstrip("/")

        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host.endswith(":80") or host.endswith(":443"):
            host = host.rsplit(":", 1)[0]

        path = unquote(parsed.path or "").strip()
        path = re.sub(r"/+", "/", path)
        path = path.rstrip("/")

        if host in {"preview.redd.it", "external-preview.redd.it"}:
            host = "i.redd.it"

        if host in {"imgur.com", "m.imgur.com", "i.imgur.com"}:
            parts = [part for part in path.split("/") if part]
            if host in {"imgur.com", "m.imgur.com"} and len(parts) >= 2 and parts[0].lower() in {"a", "gallery"}:
                album_id = re.sub(r"[^A-Za-z0-9_-]+", "", parts[1])
                if album_id:
                    return f"imgur.com/{parts[0].lower()}/{album_id.lower()}"
            elif parts:
                media_id = parts[-1].split(".", 1)[0]
                if re.fullmatch(r"[A-Za-z0-9]{5,12}", media_id or ""):
                    return f"imgur.com/{media_id.lower()}"

        if host == "v.redd.it":
            parts = [part for part in path.split("/") if part]
            path = f"/{parts[0].lower()}" if parts else ""
        elif host in {"i.redd.it", "redd.it"}:
            path = path.lower()

        return f"{host}{path}"

    def _post_media_urls(self, post: Optional[Dict[str, Any]], fallback_url: str = "") -> List[str]:
        """Return all media URLs represented by a post-like payload."""
        urls: List[str] = []
        if fallback_url:
            urls.append(str(fallback_url))

        if isinstance(post, dict):
            post_url = str(post.get("url") or "").strip()
            if post_url:
                urls.append(post_url)

            gallery_items = post.get("gallery_items")
            if isinstance(gallery_items, list):
                for item in gallery_items:
                    if isinstance(item, dict):
                        url = str(item.get("url") or "").strip()
                    else:
                        url = str(item or "").strip()
                    if url:
                        urls.append(url)

        cleaned: List[str] = []
        seen: Set[str] = set()
        for url in urls:
            normalized = self._normalize_url(url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(url)
        return cleaned

    def get_seen_media_urls(self) -> Set[str]:
        """Get set of seen media URLs (normalized)"""
        urls = self.state.get("seen_media_urls", [])
        return {self._normalize_url(u) for u in urls if isinstance(u, str)}

    def _normalize_title_signature(self, title: str) -> str:
        """Normalize titles for dedupe without overreacting to casing/punctuation."""
        if not title:
            return ""
        normalized = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", title.lower())).strip()
        if len(normalized) < 24:
            return ""
        return normalized

    def _normalize_reddit_id(self, value: str) -> str:
        """Normalize Reddit post IDs and fullnames to the bare base36 ID."""
        text = str(value or "").strip().lower()
        if not text:
            return ""

        match = re.search(r"/comments/([a-z0-9]+)", text)
        if match:
            text = match.group(1)
        elif text.startswith("t3_"):
            text = text[3:]

        text = text.split("?", 1)[0].strip().strip("/")
        return re.sub(r"[^a-z0-9_]+", "", text)

    def _normalize_permalink_signature(self, permalink: str) -> str:
        """Normalize Reddit permalinks into a stable path-only signature."""
        if not permalink:
            return ""
        value = str(permalink).strip()
        reddit_path = path_for_domain(value, "reddit.com", allow_bare_host=True)
        if reddit_path is not None:
            value = reddit_path
        return value.split("?", 1)[0].strip().rstrip("/")

    def build_post_signatures(
        self,
        post: Optional[Dict[str, Any]] = None,
        *,
        post_id: str = "",
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
    ) -> Set[str]:
        """Build content-level signatures that survive simple repost variants."""
        if post:
            post_id = str(post.get("id") or post_id or "").strip()
            url = str(post.get("url") or url or "").strip()
            title = str(post.get("title") or title or "").strip()
            permalink = str(post.get("permalink") or permalink or "").strip()
            crosspost_parent = str(post.get("crosspost_parent") or crosspost_parent or "").strip()

        signatures: Set[str] = set()
        media_urls = self._post_media_urls(post, url)
        normalized_urls = [self._normalize_url(media_url) for media_url in media_urls]
        normalized_urls = [value for value in normalized_urls if value]
        normalized_url = normalized_urls[0] if normalized_urls else self._normalize_url(url)
        normalized_title = self._normalize_title_signature(title)
        normalized_permalink = self._normalize_permalink_signature(permalink)
        normalized_crosspost = str(crosspost_parent or "").strip().lower()
        normalized_post_id = self._normalize_reddit_id(post_id)
        permalink_post_id = self._normalize_reddit_id(normalized_permalink)
        crosspost_post_id = self._normalize_reddit_id(normalized_crosspost)

        if normalized_url:
            signatures.add(f"url:{normalized_url}")
        for normalized_gallery_url in normalized_urls[1:]:
            signatures.add(f"url:{normalized_gallery_url}")
        if normalized_title:
            signatures.add(f"title:{normalized_title}")
        if normalized_permalink:
            signatures.add(f"permalink:{normalized_permalink}")
        if normalized_crosspost:
            signatures.add(f"xpost:{normalized_crosspost}")
        if crosspost_post_id:
            signatures.add(f"xpost:{crosspost_post_id}")
            signatures.add(f"id:{crosspost_post_id}")
        if normalized_title and normalized_url:
            signatures.add(f"title_url:{normalized_title}|{normalized_url}")
        if normalized_title:
            for normalized_gallery_url in normalized_urls[1:]:
                signatures.add(f"title_url:{normalized_title}|{normalized_gallery_url}")
        if normalized_post_id:
            signatures.add(f"id:{normalized_post_id}")
            signatures.add(f"fullname:t3_{normalized_post_id}")
        if permalink_post_id:
            signatures.add(f"id:{permalink_post_id}")

        return signatures

    def get_seen_signatures(self) -> Set[str]:
        """Get set of seen content signatures."""
        values = self.state.get("seen_signatures", [])
        return {str(value).strip() for value in values if str(value).strip()}

    def get_blocked_signatures(self) -> Set[str]:
        """Get permanent dedupe signatures for posted/skipped content."""
        values = self.state.get("blocked_signatures", [])
        signatures = {str(value).strip() for value in values if str(value).strip()}

        for post_id in self.get_posted_posts() | self.get_skipped_posts():
            normalized_id = self._normalize_reddit_id(post_id)
            if normalized_id:
                signatures.add(f"id:{normalized_id}")
                signatures.add(f"fullname:t3_{normalized_id}")

        for url in self.get_posted_media_urls() | self.get_skipped_media_urls():
            normalized = self._normalize_url(url)
            if normalized:
                signatures.add(f"url:{normalized}")

        return signatures

    def get_title_signatures(self, signatures: Optional[Set[str]] = None) -> List[str]:
        """Extract normalized title signatures from a signature set."""
        source = signatures if signatures is not None else self.get_blocked_signatures()
        values = []
        for signature in source:
            if not isinstance(signature, str) or not signature.startswith("title:"):
                continue
            value = signature[len("title:") :].strip()
            if value:
                values.append(value)
        return values

    def title_similarity_score(self, left: str, right: str) -> float:
        """Return a conservative similarity score for two normalized titles."""
        left_norm = self._normalize_title_signature(left)
        right_norm = self._normalize_title_signature(right)
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0

        left_tokens = set(left_norm.split())
        right_tokens = set(right_norm.split())
        if not left_tokens or not right_tokens:
            return 0.0

        intersection = left_tokens & right_tokens
        union = left_tokens | right_tokens
        jaccard = len(intersection) / max(1, len(union))
        containment = len(intersection) / max(1, min(len(left_tokens), len(right_tokens)))
        sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
        return max(sequence, jaccard, containment * 0.96)

    def find_similar_title(
        self,
        title: str,
        signatures: Optional[Set[str]] = None,
        *,
        threshold: float = 0.88,
        limit: int = 500,
    ) -> Optional[Dict[str, Any]]:
        """Find a similar saved title signature at or above the threshold."""
        normalized = self._normalize_title_signature(title)
        if not normalized:
            return None

        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = 0.88
        threshold = max(0.5, min(1.0, threshold))

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 500
        limit = max(1, min(limit, 5000))

        best_title = ""
        best_score = 0.0
        for existing in self.get_title_signatures(signatures)[-limit:]:
            score = self.title_similarity_score(normalized, existing)
            if score > best_score:
                best_title = existing
                best_score = score
            if best_score >= 1.0:
                break

        if best_score >= threshold:
            return {"title": best_title, "score": best_score}
        return None

    def mark_seen_signatures(
        self,
        *,
        post_id: str = "",
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
        post: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist dedupe signatures for a post or post-like payload."""
        for signature in self.build_post_signatures(
            post,
            post_id=post_id,
            url=url,
            title=title,
            permalink=permalink,
            crosspost_parent=crosspost_parent,
        ):
            self._append_unique("seen_signatures", signature)

    def mark_blocked_signatures(
        self,
        *,
        post_id: str = "",
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
        post: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist permanent dedupe signatures for posted or skipped content."""
        for signature in self.build_post_signatures(
            post,
            post_id=post_id,
            url=url,
            title=title,
            permalink=permalink,
            crosspost_parent=crosspost_parent,
        ):
            self._append_unique("blocked_signatures", signature)

    def backfill_seen_signatures_from_cache(self) -> int:
        """
        Recover content signatures from cached/pending payloads for items we already know
        are seen, posted, or skipped.
        """
        before = len(self.get_seen_signatures())
        seen_ids = self.get_seen_posts() | self.get_posted_posts() | self.get_skipped_posts()
        seen_urls = self.get_seen_media_urls() | self.get_posted_media_urls() | self.get_skipped_media_urls()

        pending = self.get_pending()
        if isinstance(pending, dict):
            self.mark_seen_signatures(post=pending)

        cache_root = self.state.get("cache", {})
        if isinstance(cache_root, dict):
            for cache_entry in cache_root.values():
                if not isinstance(cache_entry, dict):
                    continue
                for post in cache_entry.get("posts", []):
                    if not isinstance(post, dict):
                        continue

                    post_id = str(post.get("id") or "").strip()
                    normalized_url = self._normalize_url(str(post.get("url") or ""))
                    if post_id in seen_ids or (normalized_url and normalized_url in seen_urls):
                        self.mark_seen_signatures(post=post)

        return len(self.get_seen_signatures()) - before

    def mark_seen_media(self, url: str) -> None:
        """Mark a media URL as seen (normalized)"""
        norm = self._normalize_url(url)
        if not norm:
            return
        seen = self.get_seen_media_urls()
        if norm not in seen:
            self.state.setdefault("seen_media_urls", []).append(norm)

    def is_seen_media(self, url: str) -> bool:
        """Check if a media URL has been seen"""
        norm = self._normalize_url(url)
        if not norm:
            return False
        return norm in self.get_seen_media_urls()

    # Posted / skipped tracking
    def _append_unique(self, key: str, value: str) -> None:
        """Append a value to a list key if it is not already present."""
        if not value:
            return
        lst = self.state.setdefault(key, [])
        if value not in lst:
            lst.append(value)

    def mark_posted(
        self,
        post_id: str,
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
        post: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark a post and media URL as successfully posted."""
        self.mark_seen(post_id, url, title, permalink, crosspost_parent, post=post)
        self.mark_blocked_signatures(
            post=post,
            post_id=post_id,
            url=url,
            title=title,
            permalink=permalink,
            crosspost_parent=crosspost_parent,
        )
        self._append_unique("posted_posts", post_id)
        for media_url in self._post_media_urls(post, url):
            norm_url = self._normalize_url(media_url)
            if norm_url:
                self._append_unique("posted_media_urls", norm_url)

    def mark_skipped(
        self,
        post_id: str,
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
        post: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark a post and media URL as skipped."""
        self.mark_seen(post_id, url, title, permalink, crosspost_parent, post=post)
        self.mark_blocked_signatures(
            post=post,
            post_id=post_id,
            url=url,
            title=title,
            permalink=permalink,
            crosspost_parent=crosspost_parent,
        )
        self._append_unique("skipped_posts", post_id)
        for media_url in self._post_media_urls(post, url):
            norm_url = self._normalize_url(media_url)
            if norm_url:
                self._append_unique("skipped_media_urls", norm_url)

    def get_posted_posts(self) -> Set[str]:
        """Get set of posted post IDs."""
        return set(self.state.get("posted_posts", []))

    def get_skipped_posts(self) -> Set[str]:
        """Get set of skipped post IDs."""
        return set(self.state.get("skipped_posts", []))

    def get_posted_media_urls(self) -> Set[str]:
        """Get set of posted media URLs (normalized)."""
        urls = self.state.get("posted_media_urls", [])
        return {self._normalize_url(u) for u in urls if isinstance(u, str)}

    def get_skipped_media_urls(self) -> Set[str]:
        """Get set of skipped media URLs (normalized)."""
        urls = self.state.get("skipped_media_urls", [])
        return {self._normalize_url(u) for u in urls if isinstance(u, str)}

    # Image streak
    def get_img_streak(self) -> int:
        """Get current image streak"""
        return self.state.get("img_streak", 0)

    def increment_img_streak(self) -> None:
        """Increment image streak"""
        self.state["img_streak"] = self.get_img_streak() + 1

    def reset_img_streak(self) -> None:
        """Reset image streak to 0"""
        self.state["img_streak"] = 0

    # Subreddit streak
    def get_subreddit_streak(self) -> List[str]:
        """Get recent subreddit streak"""
        return self.state.get("subreddit_streak", [])

    def add_to_subreddit_streak(self, subreddit: str) -> None:
        """Add subreddit to streak"""
        streak = self.get_subreddit_streak()
        streak.append(subreddit)
        self.state["subreddit_streak"] = streak[-10:]  # Keep last 10

    # Pending item
    def has_pending(self) -> bool:
        """Check if there's a pending approval item"""
        return isinstance(self.state.get("pending"), dict)

    def get_pending(self) -> Optional[Dict[str, Any]]:
        """Get pending approval item"""
        return self.state.get("pending")

    def set_pending(self, pending_data: Dict[str, Any]) -> None:
        """Set pending approval item"""
        self.state["pending"] = pending_data

    def clear_pending(self) -> None:
        """Clear pending approval item"""
        self.state["pending"] = None

    # Approved post queue
    def get_post_queue(self) -> List[Dict[str, Any]]:
        """Get approved queue items in posting order."""
        queue = self.state.get("post_queue", [])
        if not isinstance(queue, list):
            queue = []
            self.state["post_queue"] = queue

        cleaned = [item for item in queue if isinstance(item, dict)]
        if len(cleaned) != len(queue):
            self.state["post_queue"] = cleaned
        return cleaned

    def get_post_queue_count(self) -> int:
        """Return number of approved queue items."""
        return len(self.get_post_queue())

    def has_queued_posts(self) -> bool:
        """Return True when there are approved posts waiting."""
        return self.get_post_queue_count() > 0

    def is_post_queued(self, post_id: str) -> bool:
        """Return True if a post id is already queued."""
        post_id = str(post_id or "").strip()
        if not post_id:
            return False
        return any(str(item.get("id") or "").strip() == post_id for item in self.get_post_queue())

    def add_to_post_queue(self, post_data: Dict[str, Any]) -> bool:
        """Append a post to the approved queue if it is not already queued."""
        post_id = str(post_data.get("id") or "").strip()
        if not post_id or self.is_post_queued(post_id):
            return False

        queued_item = dict(post_data)
        queued_item.pop("deadline_utc", None)
        queued_item["queued_at_utc"] = datetime.now(timezone.utc).isoformat()
        self.state["post_queue"] = self.get_post_queue() + [queued_item]
        self.state["post_queue"] = self.state["post_queue"][:100]
        return True

    def peek_next_queued_post(self) -> Optional[Dict[str, Any]]:
        """Return the next queued post without removing it."""
        queue = self.get_post_queue()
        return queue[0] if queue else None

    def pop_next_queued_post(self) -> Optional[Dict[str, Any]]:
        """Remove and return the next queued post."""
        queue = self.get_post_queue()
        if not queue:
            return None
        item = queue.pop(0)
        self.state["post_queue"] = queue
        return item

    def remove_queued_post(self, index: int) -> Optional[Dict[str, Any]]:
        """Remove a queued post by 1-based index."""
        queue = self.get_post_queue()
        zero_index = index - 1
        if zero_index < 0 or zero_index >= len(queue):
            return None
        item = queue.pop(zero_index)
        self.state["post_queue"] = queue
        return item

    def move_queued_post(self, index: int, direction: int) -> bool:
        """Move a queued post up or down by one slot."""
        queue = self.get_post_queue()
        zero_index = index - 1
        target_index = zero_index + direction
        if zero_index < 0 or zero_index >= len(queue) or target_index < 0 or target_index >= len(queue):
            return False

        queue[zero_index], queue[target_index] = queue[target_index], queue[zero_index]
        self.state["post_queue"] = queue
        return True

    def clear_post_queue(self) -> int:
        """Clear all approved queue items and return how many were removed."""
        count = self.get_post_queue_count()
        self.state["post_queue"] = []
        return count

    def get_queued_post_ids(self) -> Set[str]:
        """Get queued Reddit post IDs."""
        return {
            str(item.get("id") or "").strip() for item in self.get_post_queue() if str(item.get("id") or "").strip()
        }

    def get_queued_media_urls(self) -> Set[str]:
        """Get queued media URLs."""
        urls = set()
        for item in self.get_post_queue():
            for media_url in self._post_media_urls(item):
                norm = self._normalize_url(media_url)
                if norm:
                    urls.add(norm)
        return urls

    def get_queued_signatures(self) -> Set[str]:
        """Get dedupe signatures for queued posts."""
        signatures: Set[str] = set()
        for item in self.get_post_queue():
            signatures.update(self.build_post_signatures(post=item))
        return signatures

    # Admin block lists
    def normalize_author(self, author: str) -> str:
        """Normalize a Reddit author name for exact matching."""
        value = str(author or "").strip().lower()
        for prefix in ("/u/", "u/", "@"):
            if value.startswith(prefix):
                value = value[len(prefix) :].strip()
        return value

    def normalize_subreddit(self, subreddit: str) -> str:
        """Normalize a subreddit name for exact matching."""
        value = str(subreddit or "").strip().lower()
        for prefix in ("/r/", "r/"):
            if value.startswith(prefix):
                value = value[len(prefix) :].strip()
        return value

    def normalize_title_keyword(self, keyword: str) -> str:
        """Normalize a title keyword or phrase for substring matching."""
        value = str(keyword or "").strip().lower()
        value = re.sub(r"\s+", " ", value)
        return value

    def _clean_block_values(self, key: str, limit: int = 1000) -> List[str]:
        """Clean and de-duplicate a block-list state key."""
        normalizer = self.normalize_title_keyword
        if key == "blocked_authors":
            normalizer = self.normalize_author
        elif key == "blocked_subreddits":
            normalizer = self.normalize_subreddit

        cleaned: List[str] = []
        seen: Set[str] = set()
        for raw_value in self.state.get(key, []):
            value = normalizer(str(raw_value or ""))
            if value and value not in seen:
                cleaned.append(value)
                seen.add(value)

        self.state[key] = cleaned[:limit]
        return self.state[key]

    def list_blocked_authors(self) -> List[str]:
        """Return blocked authors in saved order."""
        return self._clean_block_values("blocked_authors")

    def list_blocked_subreddits(self) -> List[str]:
        """Return blocked subreddits in saved order."""
        return self._clean_block_values("blocked_subreddits")

    def list_blocked_title_keywords(self) -> List[str]:
        """Return blocked title keywords in saved order."""
        return self._clean_block_values("blocked_title_keywords")

    def get_blocked_authors(self) -> Set[str]:
        """Get blocked authors as a set."""
        return set(self.list_blocked_authors())

    def get_blocked_subreddits(self) -> Set[str]:
        """Get blocked subreddits as a set."""
        return set(self.list_blocked_subreddits())

    def get_blocked_title_keywords(self) -> Set[str]:
        """Get blocked title keywords as a set."""
        return set(self.list_blocked_title_keywords())

    def is_author_blocked(self, author: str) -> bool:
        """Return True if the author is blocked."""
        value = self.normalize_author(author)
        return bool(value and value in self.get_blocked_authors())

    def is_subreddit_blocked(self, subreddit: str) -> bool:
        """Return True if the subreddit is blocked."""
        value = self.normalize_subreddit(subreddit)
        return bool(value and value in self.get_blocked_subreddits())

    def is_title_keyword_blocked(self, title: str) -> bool:
        """Return True if any blocked keyword appears in the title."""
        value = self.normalize_title_keyword(title)
        if not value:
            return False
        return any(keyword in value for keyword in self.get_blocked_title_keywords())

    def block_author(self, author: str) -> bool:
        """Add an author to the block list. Returns True if added."""
        value = self.normalize_author(author)
        if not value or self.is_author_blocked(value):
            return False
        self.state.setdefault("blocked_authors", []).append(value)
        self._clean_block_values("blocked_authors")
        return True

    def block_subreddit(self, subreddit: str) -> bool:
        """Add a subreddit to the block list. Returns True if added."""
        value = self.normalize_subreddit(subreddit)
        if not value or self.is_subreddit_blocked(value):
            return False
        self.state.setdefault("blocked_subreddits", []).append(value)
        self._clean_block_values("blocked_subreddits")
        return True

    def block_title_keyword(self, keyword: str) -> bool:
        """Add a title keyword to the block list. Returns True if added."""
        value = self.normalize_title_keyword(keyword)
        if not value or value in self.get_blocked_title_keywords():
            return False
        self.state.setdefault("blocked_title_keywords", []).append(value)
        self._clean_block_values("blocked_title_keywords")
        return True

    def clear_blocklist(self, block_type: str = "all") -> Optional[int]:
        """Clear one or all admin block lists and return the number removed."""
        normalized = str(block_type or "all").strip().lower()
        key_map = {
            "author": "blocked_authors",
            "authors": "blocked_authors",
            "sub": "blocked_subreddits",
            "subs": "blocked_subreddits",
            "subreddit": "blocked_subreddits",
            "subreddits": "blocked_subreddits",
            "keyword": "blocked_title_keywords",
            "keywords": "blocked_title_keywords",
            "title": "blocked_title_keywords",
            "titles": "blocked_title_keywords",
        }

        keys = [
            "blocked_authors",
            "blocked_subreddits",
            "blocked_title_keywords",
        ]
        if normalized != "all":
            key = key_map.get(normalized)
            if not key:
                return None
            keys = [key]

        count = 0
        for key in keys:
            count += len(self.state.get(key, []))
            self.state[key] = []
        return count

    def get_block_counts(self) -> Dict[str, int]:
        """Return counts for admin block lists."""
        return {
            "authors": len(self.list_blocked_authors()),
            "subreddits": len(self.list_blocked_subreddits()),
            "keywords": len(self.list_blocked_title_keywords()),
        }

    # Telegram update offset
    def get_update_offset(self) -> int:
        """Get Telegram update offset"""
        return self.state.get("tg_update_offset", 0)

    def set_update_offset(self, offset: int) -> None:
        """Set Telegram update offset"""
        self.state["tg_update_offset"] = offset

    # Last tick
    def get_last_tick(self) -> Optional[str]:
        """Get last tick timestamp (ISO format)"""
        return self.state.get("last_tick_utc")

    def set_last_tick(self, timestamp: str) -> None:
        """Set last tick timestamp"""
        self.state["last_tick_utc"] = timestamp

    # Reddit backoff
    def get_reddit_backoff(self) -> Optional[str]:
        """Get Reddit backoff deadline (ISO format)"""
        return self.state.get("reddit_backoff_until_utc")

    def set_reddit_backoff(self, until: str) -> None:
        """Set Reddit backoff deadline"""
        self.state["reddit_backoff_until_utc"] = until

    def clear_reddit_backoff(self) -> None:
        """Clear Reddit backoff"""
        self.state["reddit_backoff_until_utc"] = None

    # Preview times
    def get_preview_times(self) -> List[str]:
        """Get recent preview timestamps"""
        return self.state.get("preview_times_utc", [])

    def add_preview_time(self, timestamp: str) -> None:
        """Add preview timestamp"""
        times = self.get_preview_times()
        times.append(timestamp)
        self.state["preview_times_utc"] = times[-50:]  # Keep last 50

    def set_preview_times(self, times: List[str]) -> None:
        """Set preview times list"""
        self.state["preview_times_utc"] = times

    # Cache
    def get_cache(self, key: str) -> Optional[Dict[str, Any]]:
        """Get cached data by key"""
        return self.state.get("cache", {}).get(key)

    def set_cache(self, key: str, data: Dict[str, Any]) -> None:
        """Set cached data"""
        if "cache" not in self.state:
            self.state["cache"] = {}
        self.state["cache"][key] = data

    def clear_cache(self) -> None:
        """Clear all cache"""
        self.state["cache"] = {}

    # Post history
    def add_to_history(self, post_data: Dict[str, Any]) -> None:
        """Add post to history"""
        if "post_history" not in self.state:
            self.state["post_history"] = []

        self.state["post_history"].append(post_data)
        self.state["post_history"] = self.state["post_history"][-100:]  # Keep last 100

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent post history"""
        history = self.state.get("post_history", [])
        return history[-limit:]

    # Author cooldown history
    def record_author_post(self, post_data: Dict[str, Any]) -> None:
        """Record a successful post author for optional cooldown filtering."""
        author = self.normalize_author(str(post_data.get("author") or ""))
        if not author:
            return

        entry = {
            "author": author,
            "subreddit": str(post_data.get("subreddit") or "").strip(),
            "post_id": str(post_data.get("id") or post_data.get("reddit_id") or "").strip(),
            "posted_at_utc": str(post_data.get("posted_at_utc") or datetime.now(timezone.utc).isoformat()),
        }
        history = self.state.setdefault("author_history", [])
        if not isinstance(history, list):
            history = []
            self.state["author_history"] = history
        history.append(entry)
        self.state["author_history"] = [item for item in history if isinstance(item, dict)][-2000:]

    def get_author_cooldown_match(self, author: str, cooldown_hours: int) -> Optional[Dict[str, Any]]:
        """Return the most recent matching author post inside the cooldown window."""
        normalized = self.normalize_author(author)
        if not normalized:
            return None

        try:
            hours = int(cooldown_hours)
        except (TypeError, ValueError):
            hours = 0
        if hours <= 0:
            return None

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        candidates: List[Dict[str, Any]] = []

        for item in self.state.get("author_history", []):
            if isinstance(item, dict):
                candidates.append(item)

        for item in self.state.get("post_analytics", []):
            if not isinstance(item, dict):
                continue
            if item.get("author"):
                candidates.append(
                    {
                        "author": item.get("author"),
                        "subreddit": item.get("subreddit", ""),
                        "post_id": item.get("reddit_id", ""),
                        "posted_at_utc": item.get("posted_at_utc"),
                    }
                )

        best: Optional[Dict[str, Any]] = None
        best_time: Optional[datetime] = None
        for item in candidates:
            if self.normalize_author(str(item.get("author") or "")) != normalized:
                continue
            posted_at = self._parse_utc_datetime(item.get("posted_at_utc"))
            if not posted_at or posted_at < cutoff:
                continue
            if best_time is None or posted_at > best_time:
                best_time = posted_at
                best = dict(item)

        if not best or best_time is None:
            return None

        best["age_hours"] = max(0.0, (now - best_time).total_seconds() / 3600.0)
        best["cooldown_hours"] = hours
        return best

    # Performance analytics
    def _parse_utc_datetime(self, value: Any) -> Optional[datetime]:
        """Parse an ISO timestamp and normalize it to UTC."""
        if not isinstance(value, str) or not value.strip():
            return None

        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _coerce_int(self, value: Any) -> Optional[int]:
        """Convert Telegram/Reddit metric values into integers when possible."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _coerce_float(self, value: Any) -> Optional[float]:
        """Convert optional scoring values into floats when possible."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalize_analytics_channel(self, value: Any) -> str:
        """Normalize Telegram channel identifiers for matching updates."""
        return str(value or "").strip().lower()

    def _telegram_channel_candidates(self, message: Dict[str, Any]) -> Set[str]:
        """Return possible channel identifiers for a Telegram message update."""
        chat = message.get("chat") if isinstance(message, dict) else {}
        if not isinstance(chat, dict):
            chat = {}

        candidates: Set[str] = set()
        chat_id = chat.get("id")
        if chat_id is not None:
            candidates.add(self._normalize_analytics_channel(chat_id))

        username = str(chat.get("username") or "").strip()
        if username:
            username = username.lstrip("@")
            candidates.add(self._normalize_analytics_channel(username))
            candidates.add(self._normalize_analytics_channel(f"@{username}"))

        return {value for value in candidates if value}

    def _extract_telegram_metrics(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Extract optional performance metrics from a Telegram message payload."""
        if not isinstance(message, dict):
            return {}

        metrics: Dict[str, Any] = {}
        views = self._coerce_int(message.get("views", message.get("view_count")))
        forwards = self._coerce_int(message.get("forwards", message.get("forward_count")))

        if views is not None:
            metrics["views"] = views
        if forwards is not None:
            metrics["forwards"] = forwards

        reactions: Dict[str, int] = {}
        reaction_total: Optional[int] = None
        reaction_data = message.get("reactions")

        reaction_items: List[Dict[str, Any]] = []
        if isinstance(reaction_data, dict):
            reaction_total = self._coerce_int(reaction_data.get("total_count"))
            raw_items = reaction_data.get("reactions", [])
            if isinstance(raw_items, list):
                reaction_items = [item for item in raw_items if isinstance(item, dict)]
        elif isinstance(reaction_data, list):
            reaction_items = [item for item in reaction_data if isinstance(item, dict)]

        for item in reaction_items:
            count = self._coerce_int(item.get("total_count", item.get("count")))
            if count is None:
                continue

            reaction_type = item.get("type")
            key = ""
            if isinstance(reaction_type, dict):
                key = str(
                    reaction_type.get("emoji")
                    or reaction_type.get("custom_emoji_id")
                    or reaction_type.get("type")
                    or ""
                ).strip()
            else:
                key = str(item.get("emoji") or item.get("reaction") or reaction_type or "").strip()

            if not key:
                key = "reaction"
            reactions[key] = reactions.get(key, 0) + count

        if reaction_total is None and reactions:
            reaction_total = sum(reactions.values())
        if reaction_total is None:
            reaction_total = self._coerce_int(message.get("reaction_count", message.get("reactions_count")))

        if reactions:
            metrics["reactions"] = reactions
        if reaction_total is not None:
            metrics["reaction_total"] = reaction_total

        return metrics

    def record_post_analytics(
        self,
        post_data: Dict[str, Any],
        *,
        channel: str = "",
        message_id: Optional[int] = None,
        telegram_message: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a successful channel post for performance reporting."""
        records = self.state.setdefault("post_analytics", [])
        if not isinstance(records, list):
            records = []
            self.state["post_analytics"] = records

        now_iso = datetime.now(timezone.utc).isoformat()
        message = telegram_message if isinstance(telegram_message, dict) else {}
        telegram_message_id = self._coerce_int(message.get("message_id"))
        final_message_id = message_id if isinstance(message_id, int) else telegram_message_id

        posted_at = now_iso
        telegram_date = self._coerce_int(message.get("date"))
        if telegram_date is not None:
            try:
                posted_at = datetime.fromtimestamp(telegram_date, tz=timezone.utc).isoformat()
            except (OSError, OverflowError, ValueError):
                posted_at = now_iso

        reddit_id = str(post_data.get("id") or post_data.get("reddit_id") or "").strip()
        selection_score = (
            post_data.get("_score")
            if post_data.get("_score") is not None
            else post_data.get("selection_score", post_data.get("score"))
        )

        entry: Dict[str, Any] = {
            "reddit_id": reddit_id,
            "channel": str(channel or "").strip(),
            "message_id": final_message_id,
            "subreddit": str(post_data.get("subreddit") or "unknown").strip() or "unknown",
            "type": str(post_data.get("type") or "unknown").strip() or "unknown",
            "title": str(post_data.get("title") or "").strip(),
            "url": str(post_data.get("url") or "").strip(),
            "permalink": str(post_data.get("permalink") or "").strip(),
            "author": str(post_data.get("author") or "").strip(),
            "posted_at_utc": posted_at,
            "created_utc": post_data.get("created_utc"),
            "queued_at_utc": post_data.get("queued_at_utc"),
            "selection_score": self._coerce_float(selection_score),
            "upvotes": self._coerce_int(post_data.get("upvotes", post_data.get("ups"))),
            "comments": self._coerce_int(post_data.get("num_comments", post_data.get("comments"))),
            "metrics_updated_at_utc": None,
        }

        metrics = self._extract_telegram_metrics(message)
        if metrics:
            entry.update(metrics)
            entry["metrics_updated_at_utc"] = now_iso

        match_index: Optional[int] = None
        normalized_channel = self._normalize_analytics_channel(entry["channel"])
        for index, existing in enumerate(records):
            if not isinstance(existing, dict):
                continue
            existing_message_id = self._coerce_int(existing.get("message_id"))
            existing_channel = self._normalize_analytics_channel(existing.get("channel"))
            if (
                final_message_id is not None
                and existing_message_id == final_message_id
                and (not normalized_channel or not existing_channel or normalized_channel == existing_channel)
            ):
                match_index = index
                break
            if reddit_id and str(existing.get("reddit_id") or "").strip() == reddit_id:
                match_index = index
                break

        if match_index is None:
            records.append(entry)
            self.state["post_analytics"] = records[-2000:]
            return entry

        existing = dict(records[match_index])
        for key, value in entry.items():
            if value not in (None, "") or key in {"metrics_updated_at_utc"}:
                existing[key] = value
        records[match_index] = existing
        self.state["post_analytics"] = records[-2000:]
        return existing

    def update_post_analytics_from_telegram_message(self, message: Dict[str, Any]) -> bool:
        """Update tracked post metrics from a Telegram channel post update."""
        records = self.state.get("post_analytics", [])
        if not isinstance(records, list):
            return False

        message_id = self._coerce_int(message.get("message_id") if isinstance(message, dict) else None)
        if message_id is None:
            return False

        metrics = self._extract_telegram_metrics(message)
        if not metrics:
            return False

        channel_candidates = self._telegram_channel_candidates(message)
        for index in range(len(records) - 1, -1, -1):
            record = records[index]
            if not isinstance(record, dict):
                continue
            if self._coerce_int(record.get("message_id")) != message_id:
                continue

            record_channel = self._normalize_analytics_channel(record.get("channel"))
            if channel_candidates and record_channel and record_channel not in channel_candidates:
                continue

            record.update(metrics)
            record["metrics_updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            records[index] = record
            return True

        return False

    def get_post_analytics(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return recent tracked post analytics, newest first."""
        records = [item for item in self.state.get("post_analytics", []) if isinstance(item, dict)]
        if limit and limit > 0:
            records = records[-limit:]
        return list(reversed(records))

    def get_analytics_summary(self, days: int = 7) -> Dict[str, Any]:
        """Summarize tracked post performance for a rolling day window."""
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 3650))

        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        records: List[Dict[str, Any]] = []

        for record in self.state.get("post_analytics", []):
            if not isinstance(record, dict):
                continue
            posted_at = self._parse_utc_datetime(record.get("posted_at_utc"))
            if posted_at and posted_at < since:
                continue
            records.append(record)

        subreddit_buckets: Dict[str, Dict[str, Any]] = {}
        type_buckets: Dict[str, Dict[str, Any]] = {}
        hour_buckets: Dict[str, Dict[str, Any]] = {}
        view_values: List[int] = []
        forward_values: List[int] = []
        reaction_values: List[int] = []
        score_values: List[float] = []

        def add_bucket(bucket: Dict[str, Dict[str, Any]], key: str, record: Dict[str, Any]) -> None:
            item = bucket.setdefault(
                key,
                {
                    "key": key,
                    "count": 0,
                    "views": 0,
                    "view_records": 0,
                    "forwards": 0,
                    "forward_records": 0,
                    "reactions": 0,
                    "reaction_records": 0,
                },
            )
            item["count"] += 1

            views = self._coerce_int(record.get("views"))
            if views is not None:
                item["views"] += views
                item["view_records"] += 1

            forwards = self._coerce_int(record.get("forwards"))
            if forwards is not None:
                item["forwards"] += forwards
                item["forward_records"] += 1

            reactions = self._coerce_int(record.get("reaction_total"))
            if reactions is not None:
                item["reactions"] += reactions
                item["reaction_records"] += 1

        for record in records:
            subreddit = str(record.get("subreddit") or "unknown").strip() or "unknown"
            media_type = str(record.get("type") or "unknown").strip() or "unknown"
            posted_at = self._parse_utc_datetime(record.get("posted_at_utc"))
            hour_key = f"{posted_at.hour:02d}:00" if posted_at else "unknown"

            add_bucket(subreddit_buckets, subreddit, record)
            add_bucket(type_buckets, media_type, record)
            add_bucket(hour_buckets, hour_key, record)

            views = self._coerce_int(record.get("views"))
            if views is not None:
                view_values.append(views)
            forwards = self._coerce_int(record.get("forwards"))
            if forwards is not None:
                forward_values.append(forwards)
            reactions = self._coerce_int(record.get("reaction_total"))
            if reactions is not None:
                reaction_values.append(reactions)
            score = self._coerce_float(record.get("selection_score"))
            if score is not None:
                score_values.append(score)

        def sorted_buckets(bucket: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
            return sorted(
                bucket.values(),
                key=lambda item: (item["count"], item["views"], item["reactions"]),
                reverse=True,
            )

        top_by_views = sorted(
            [record for record in records if self._coerce_int(record.get("views")) is not None],
            key=lambda record: self._coerce_int(record.get("views")) or 0,
            reverse=True,
        )[:5]
        top_by_reactions = sorted(
            [record for record in records if self._coerce_int(record.get("reaction_total")) is not None],
            key=lambda record: self._coerce_int(record.get("reaction_total")) or 0,
            reverse=True,
        )[:5]

        return {
            "days": days,
            "since_utc": since.isoformat(),
            "total_posts": len(records),
            "subreddits": sorted_buckets(subreddit_buckets),
            "media_types": sorted_buckets(type_buckets),
            "posting_hours": sorted_buckets(hour_buckets),
            "total_views": sum(view_values),
            "view_records": len(view_values),
            "avg_views": (sum(view_values) / len(view_values)) if view_values else None,
            "total_forwards": sum(forward_values),
            "forward_records": len(forward_values),
            "avg_forwards": (sum(forward_values) / len(forward_values)) if forward_values else None,
            "total_reactions": sum(reaction_values),
            "reaction_records": len(reaction_values),
            "avg_reactions": (sum(reaction_values) / len(reaction_values)) if reaction_values else None,
            "avg_selection_score": (sum(score_values) / len(score_values)) if score_values else None,
            "top_by_views": top_by_views,
            "top_by_reactions": top_by_reactions,
        }

    def _format_analytics_bucket(self, item: Dict[str, Any]) -> str:
        """Format one analytics bucket row."""
        text = f"{item['key']}: {item['count']}"
        extras = []
        if item.get("view_records"):
            extras.append(f"{item['views']} views")
        if item.get("reaction_records"):
            extras.append(f"{item['reactions']} reactions")
        if item.get("forward_records"):
            extras.append(f"{item['forwards']} forwards")
        if extras:
            text += f" ({', '.join(extras)})"
        return text

    def build_analytics_report(self, days: int = 7, limit: int = 5) -> str:
        """Build a compact text report for Telegram commands and the CLI."""
        summary = self.get_analytics_summary(days)
        limit = max(1, min(int(limit or 5), 20))

        lines = [
            "Performance Analytics",
            f"Window: last {summary['days']} day(s)",
            f"Tracked posts: {summary['total_posts']}",
        ]

        if summary["total_posts"] <= 0:
            lines.append("")
            lines.append("No tracked channel posts in this window yet.")
            lines.append("New successful posts will be recorded automatically.")
            return "\n".join(lines)

        if summary["avg_selection_score"] is not None:
            lines.append(f"Avg selection score: {summary['avg_selection_score']:.3f}")

        metric_parts = []
        if summary["view_records"]:
            metric_parts.append(f"views {summary['total_views']} total / {summary['avg_views']:.1f} avg")
        if summary["reaction_records"]:
            metric_parts.append(f"reactions {summary['total_reactions']} total / {summary['avg_reactions']:.1f} avg")
        if summary["forward_records"]:
            metric_parts.append(f"forwards {summary['total_forwards']} total / {summary['avg_forwards']:.1f} avg")
        if metric_parts:
            lines.append("Known metrics: " + "; ".join(metric_parts))
        else:
            lines.append("Known metrics: none yet from Telegram updates")

        lines.append("")
        lines.append("Top subreddits:")
        for item in summary["subreddits"][:limit]:
            lines.append(f"  - r/{self._format_analytics_bucket(item)}")

        lines.append("")
        lines.append("Media types:")
        for item in summary["media_types"][:limit]:
            lines.append(f"  - {self._format_analytics_bucket(item)}")

        lines.append("")
        lines.append("Busiest posting hours (UTC):")
        for item in summary["posting_hours"][:limit]:
            lines.append(f"  - {self._format_analytics_bucket(item)}")

        if summary["top_by_views"]:
            lines.append("")
            lines.append("Top posts by views:")
            for record in summary["top_by_views"][:limit]:
                title = str(record.get("title") or "").strip() or "(untitled)"
                if len(title) > 54:
                    title = title[:51] + "..."
                lines.append(f"  - {record.get('views', 0)} views | r/{record.get('subreddit', '?')} | {title}")

        if summary["top_by_reactions"]:
            lines.append("")
            lines.append("Top posts by reactions:")
            for record in summary["top_by_reactions"][:limit]:
                title = str(record.get("title") or "").strip() or "(untitled)"
                if len(title) > 54:
                    title = title[:51] + "..."
                lines.append(
                    f"  - {record.get('reaction_total', 0)} reactions | r/{record.get('subreddit', '?')} | {title}"
                )

        lines.append("")
        lines.append("Views, forwards, and reactions are filled when Telegram provides them in channel updates.")
        return "\n".join(lines)

    # Daily posts
    def _daily_date_key(self, date_key: Optional[str] = None) -> str:
        """Return a normalized date key for daily counters."""
        value = str(date_key or "").strip()
        if value:
            return value
        return datetime.now(timezone.utc).date().isoformat()

    def _ensure_daily_counters(self, date_key: Optional[str] = None) -> str:
        """Reset daily counters when the active date key changes."""
        key = self._daily_date_key(date_key)
        if self.state.get("daily_posts_date") != key:
            self.state["daily_posts_count"] = 0
            self.state["daily_skips_count"] = 0
            self.state["daily_failures_count"] = 0
            self.state["daily_posts_date"] = key
        return key

    def get_daily_posts_count(self, date_key: Optional[str] = None) -> int:
        """Get number of posts for the active daily counter date."""
        self._ensure_daily_counters(date_key)
        return int(self.state.get("daily_posts_count", 0) or 0)

    def increment_daily_posts(self, date_key: Optional[str] = None) -> None:
        """Increment daily post counter."""
        self._ensure_daily_counters(date_key)
        self.state["daily_posts_count"] = self.get_daily_posts_count(date_key) + 1

    def get_daily_skips_count(self, date_key: Optional[str] = None) -> int:
        """Get number of skips for the active daily counter date."""
        self._ensure_daily_counters(date_key)
        return int(self.state.get("daily_skips_count", 0) or 0)

    def increment_daily_skips(self, date_key: Optional[str] = None) -> None:
        """Increment daily skip counter."""
        self._ensure_daily_counters(date_key)
        self.state["daily_skips_count"] = self.get_daily_skips_count(date_key) + 1

    def get_daily_failures_count(self, date_key: Optional[str] = None) -> int:
        """Get number of failures for the active daily counter date."""
        self._ensure_daily_counters(date_key)
        return int(self.state.get("daily_failures_count", 0) or 0)

    def increment_daily_failures(self, date_key: Optional[str] = None) -> None:
        """Increment daily failure counter."""
        self._ensure_daily_counters(date_key)
        self.state["daily_failures_count"] = self.get_daily_failures_count(date_key) + 1

    def _normalize_error_category(self, value: Any) -> str:
        """Return a compact category key for an error-history event."""
        normalized = str(value or "runtime").strip().lower().replace("-", "_")
        normalized = re.sub(r"[^a-z0-9_]+", "_", normalized).strip("_")
        return (normalized or "runtime")[:60]

    def _normalize_error_history_item(self, item: Any) -> Optional[Dict[str, Any]]:
        """Normalize one stored error-history item."""
        if not isinstance(item, dict):
            return None

        message = re.sub(r"\s+", " ", str(item.get("message") or item.get("reason") or "").strip())
        if not message:
            return None

        raw_timestamp = item.get("timestamp_utc") or item.get("timestamp")
        parsed = self._parse_event_time(raw_timestamp)
        timestamp = parsed.isoformat() if parsed else datetime.now(timezone.utc).isoformat()

        event = {
            "timestamp_utc": timestamp,
            "category": self._normalize_error_category(item.get("category") or "runtime"),
            "message": message[:500],
        }

        source = str(item.get("source") or "").strip()
        if source:
            event["source"] = source[:80]

        return event

    def _trim_error_history(self) -> List[Dict[str, Any]]:
        """Keep persistent error history bounded and normalized."""
        history: List[Dict[str, Any]] = []
        for item in list(self.state.get("error_history", [])):
            normalized = self._normalize_error_history_item(item)
            if normalized:
                history.append(normalized)

        history = history[-self.ERROR_HISTORY_LIMIT :]
        self.state["error_history"] = history
        return history

    def _append_error_history(
        self,
        message: str,
        *,
        category: str = "runtime",
        source: str = "",
        timestamp_utc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append a normalized event to persistent error history."""
        event = self._normalize_error_history_item(
            {
                "timestamp_utc": timestamp_utc or datetime.now(timezone.utc).isoformat(),
                "category": category,
                "source": source,
                "message": message,
            }
        )
        if not event:
            return {}

        history = self._trim_error_history()
        history.append(event)
        self.state["error_history"] = history[-self.ERROR_HISTORY_LIMIT :]
        return event

    def record_failure(self, reason: str, date_key: Optional[str] = None) -> None:
        """Record a runtime failure for daily digest reporting."""
        self.increment_daily_failures(date_key)
        message = str(reason or "Unknown error").strip()[:500]
        timestamp = datetime.now(timezone.utc).isoformat()
        self.state["last_error"] = message
        self.state["last_error_utc"] = timestamp
        self._append_error_history(
            message,
            category="runtime",
            source="record_failure",
            timestamp_utc=timestamp,
        )

    def get_last_error(self) -> Optional[Dict[str, Any]]:
        """Return the last recorded runtime error, if present."""
        message = str(self.state.get("last_error") or "").strip()
        if not message:
            return None
        return {
            "message": message,
            "timestamp_utc": self.state.get("last_error_utc"),
        }

    def get_recent_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return recent persistent errors, newest first."""
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 10

        history = list(reversed(self._trim_error_history()))
        if not history:
            last_error = self.get_last_error()
            if last_error:
                return [
                    {
                        "timestamp_utc": last_error.get("timestamp_utc"),
                        "category": "runtime",
                        "message": last_error.get("message"),
                        "source": "last_error",
                    }
                ][:limit]
        return history[:limit]

    def clear_recent_errors(self) -> int:
        """Clear persistent error history and the last-error pointer."""
        history = self._trim_error_history()
        cleared = len(history)
        if cleared == 0 and self.get_last_error():
            cleared = 1
        self.state["error_history"] = []
        self.state["last_error"] = None
        self.state["last_error_utc"] = None
        return cleared

    def _normalize_recovery_event(self, item: Any) -> Optional[Dict[str, Any]]:
        """Normalize one stored auto-recovery event."""
        if not isinstance(item, dict):
            return None

        action = self._normalize_error_category(item.get("action") or "recovery")
        category = self._normalize_error_category(item.get("category") or "runtime")
        detail = re.sub(r"\s+", " ", str(item.get("detail") or "").strip())
        if not detail:
            detail = action.replace("_", " ")

        raw_timestamp = item.get("timestamp_utc") or item.get("timestamp")
        parsed = self._parse_event_time(raw_timestamp)
        timestamp = parsed.isoformat() if parsed else datetime.now(timezone.utc).isoformat()

        level = str(item.get("level") or "warn").strip().lower()
        if level not in {"info", "warn", "error"}:
            level = "warn"

        event = {
            "timestamp_utc": timestamp,
            "level": level,
            "action": action,
            "category": category,
            "detail": detail[:500],
        }

        for key in ("post_id", "subreddit", "media_type"):
            value = str(item.get(key) or "").strip()
            if value:
                event[key] = value[:120]

        return event

    def _trim_recovery_events(self) -> List[Dict[str, Any]]:
        """Keep auto-recovery event history bounded and normalized."""
        events: List[Dict[str, Any]] = []
        for item in list(self.state.get("recovery_events", [])):
            event = self._normalize_recovery_event(item)
            if event:
                events.append(event)
        events = events[-self.RECOVERY_HISTORY_LIMIT :]
        self.state["recovery_events"] = events
        return events

    def record_recovery_event(
        self,
        action: str,
        detail: str,
        *,
        category: str = "runtime",
        level: str = "warn",
        post: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record an auto-recovery action or failure."""
        post = post or {}
        event = self._normalize_recovery_event(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "action": action,
                "category": category,
                "detail": detail,
                "post_id": post.get("id"),
                "subreddit": post.get("subreddit"),
                "media_type": post.get("type"),
            }
        )
        if not event:
            return {}
        events = self._trim_recovery_events()
        events.append(event)
        self.state["recovery_events"] = events[-self.RECOVERY_HISTORY_LIMIT :]
        return event

    def get_recovery_events(
        self,
        limit: int = 10,
        window_minutes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent auto-recovery events, newest first."""
        try:
            limit = max(1, min(int(limit), self.RECOVERY_HISTORY_LIMIT))
        except (TypeError, ValueError):
            limit = 10

        events = self._trim_recovery_events()
        if window_minutes and window_minutes > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
            events = [
                item for item in events if (self._parse_event_time(item.get("timestamp_utc")) or cutoff) >= cutoff
            ]
        return list(reversed(events))[:limit]

    def get_recovery_failure_count(self, window_minutes: int = 30) -> int:
        """Return recent warn/error recovery event count."""
        return sum(
            1
            for event in self.get_recovery_events(
                self.RECOVERY_HISTORY_LIMIT,
                window_minutes=window_minutes,
            )
            if str(event.get("level") or "").lower() in {"warn", "error"}
        )

    def get_recovery_last_notice_utc(self) -> Optional[str]:
        """Return last auto-recovery admin notice timestamp."""
        value = self.state.get("recovery_last_notice_utc")
        return str(value).strip() if value else None

    def mark_recovery_notice_sent(self) -> None:
        """Persist that an auto-recovery admin notice was sent."""
        self.state["recovery_last_notice_utc"] = datetime.now(timezone.utc).isoformat()

    def clear_recovery_events(self) -> int:
        """Clear auto-recovery event history and notice cooldown."""
        count = len(self._trim_recovery_events())
        self.state["recovery_events"] = []
        self.state["recovery_last_notice_utc"] = None
        return count

    # Emergency pause
    def _parse_event_time(self, value: Any) -> Optional[datetime]:
        """Parse an ISO timestamp as a UTC-aware datetime."""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _trim_emergency_failures(self, window_minutes: Optional[int] = None) -> List[Dict[str, Any]]:
        """Keep emergency failure history bounded, optionally by rolling window."""
        failures = [item for item in self.state.get("emergency_failures", []) if isinstance(item, dict)]

        if window_minutes and window_minutes > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
            failures = [
                item for item in failures if (self._parse_event_time(item.get("timestamp_utc")) or cutoff) >= cutoff
            ]

        failures = failures[-500:]
        self.state["emergency_failures"] = failures
        return failures

    def record_emergency_failure(
        self,
        category: str,
        reason: str,
        window_minutes: int,
    ) -> Dict[str, int]:
        """Record a categorized failure and return rolling counts by category."""
        normalized = str(category or "runtime").strip().lower().replace("-", "_")
        reason_text = str(reason or "Unknown failure").strip()[:500]
        timestamp = datetime.now(timezone.utc).isoformat()
        event = {
            "category": normalized,
            "reason": reason_text,
            "timestamp_utc": timestamp,
        }
        self.state.setdefault("emergency_failures", [])
        if not isinstance(self.state["emergency_failures"], list):
            self.state["emergency_failures"] = []
        self.state["emergency_failures"].append(event)
        self._append_error_history(
            reason_text,
            category=normalized,
            source="emergency",
            timestamp_utc=timestamp,
        )
        return self.get_emergency_failure_counts(window_minutes)

    def get_emergency_failures(
        self,
        window_minutes: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent emergency failure events, newest first."""
        return list(reversed(self._trim_emergency_failures(window_minutes)))

    def get_emergency_failure_counts(
        self,
        window_minutes: Optional[int] = None,
    ) -> Dict[str, int]:
        """Return rolling emergency failure counts by category."""
        counts: Dict[str, int] = {}
        for event in self._trim_emergency_failures(window_minutes):
            category = str(event.get("category") or "unknown").strip().lower()
            if not category:
                category = "unknown"
            counts[category] = counts.get(category, 0) + 1
        return counts

    def clear_emergency_failures(self, category: Optional[str] = None) -> None:
        """Clear emergency failure history, optionally for one category."""
        if not category:
            self.state["emergency_failures"] = []
            return
        normalized = str(category).strip().lower().replace("-", "_")
        self.state["emergency_failures"] = [
            item
            for item in self.state.get("emergency_failures", [])
            if not isinstance(item, dict) or str(item.get("category") or "").strip().lower() != normalized
        ]

    def get_emergency_pause(self) -> Optional[Dict[str, Any]]:
        """Return active emergency pause details, if any."""
        pause = self.state.get("emergency_pause")
        if not isinstance(pause, dict) or not pause.get("active"):
            return None
        return dict(pause)

    def set_emergency_pause(
        self,
        *,
        category: str,
        reason: str,
        count: int,
        threshold: int,
        window_minutes: int,
    ) -> None:
        """Activate an emergency pause and store its trigger details."""
        self.state["paused"] = True
        self.state["emergency_pause"] = {
            "active": True,
            "category": str(category or "unknown"),
            "reason": str(reason or "Unknown failure").strip()[:500],
            "triggered_at_utc": datetime.now(timezone.utc).isoformat(),
            "count": max(0, int(count or 0)),
            "threshold": max(0, int(threshold or 0)),
            "window_minutes": max(0, int(window_minutes or 0)),
        }

    def clear_emergency_pause(self) -> None:
        """Clear active emergency pause details."""
        self.state["emergency_pause"] = {
            "active": False,
            "category": None,
            "reason": None,
            "triggered_at_utc": None,
            "count": 0,
            "threshold": 0,
            "window_minutes": 0,
        }

    # Pause state
    def is_paused(self) -> bool:
        """Check if bot is paused"""
        return self.state.get("paused", False)

    def set_paused(self, paused: bool) -> None:
        """Set pause state"""
        self.state["paused"] = paused
        if not paused:
            self.clear_emergency_pause()
            self.clear_emergency_failures()

    # Statistics
    def get_stats(self) -> Dict[str, Any]:
        """Get bot statistics"""
        return self.state.get("stats", {})

    def increment_stat(self, stat_name: str) -> None:
        """Increment a statistic counter"""
        if "stats" not in self.state:
            self.state["stats"] = {}

        current = self.state["stats"].get(stat_name, 0)
        self.state["stats"][stat_name] = current + 1

    def get_approval_rate(self) -> float:
        """Calculate approval rate percentage"""
        stats = self.get_stats()
        approvals = stats.get("total_approvals", 0)
        skips = stats.get("total_skips", 0)
        total = approvals + skips

        if total == 0:
            return 0.0

        return (approvals / total) * 100
