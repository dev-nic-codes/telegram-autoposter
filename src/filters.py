"""
Content filtering logic.
Determines which posts are acceptable based on rules.
"""

import math
import random
import string
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
from utils import log


class ContentFilter:
    """Filters Reddit posts based on configured rules"""

    def __init__(self, config):
        """
        Initialize filter with configuration.

        Args:
            config: Config object with filter settings
        """
        self.config = config

    def _effective_rule(self, subreddit: str) -> Dict[str, Any]:
        """Return merged per-subreddit rules with a global-config fallback."""
        getter = getattr(self.config, "get_effective_subreddit_rule", None)
        if callable(getter):
            return getter(subreddit)
        return {
            "min_upvotes": getattr(self.config, "min_upvotes", 0),
            "max_post_age_hours": getattr(self.config, "max_post_age_hours", 0),
            "media_type": "any",
            "skip_nsfw": getattr(self.config, "skip_nsfw", True),
            "caption_footer": "",
            "priority_weight": 1.0,
        }

    def should_skip_post(
        self,
        post: Dict[str, Any],
        reason_list: Optional[List[str]] = None,
        *,
        ignore_age: bool = False,
    ) -> bool:
        """
        Determine if post should be skipped.

        Args:
            post: Post dictionary from Reddit
            reason_list: Optional list to append skip reasons to

        Returns:
            True if post should be skipped
        """
        rule = self._effective_rule(post.get("subreddit", ""))
        min_upvotes = int(rule.get("min_upvotes", self.config.min_upvotes) or 0)
        max_post_age_hours = int(rule.get("max_post_age_hours", self.config.max_post_age_hours) or 0)
        skip_nsfw = bool(rule.get("skip_nsfw", self.config.skip_nsfw))
        media_type = str(rule.get("media_type", "any") or "any")

        # NSFW filter
        if skip_nsfw and post.get("nsfw", False):
            if reason_list is not None:
                reason_list.append("NSFW content")
            return True

        # Minimum upvotes
        if post.get("upvotes", 0) < min_upvotes:
            if reason_list is not None:
                reason_list.append(f"Low upvotes: {post.get('upvotes', 0)} < {min_upvotes}")
            return True

        # Per-subreddit media type
        if media_type in {"image", "video", "gallery"} and post.get("type") != media_type:
            if reason_list is not None:
                reason_list.append(f"Media type rule: {media_type} only")
            return True

        if post.get("type") == "gallery":
            if not bool(getattr(self.config, "gallery_posts_enabled", True)):
                if reason_list is not None:
                    reason_list.append("Gallery posts disabled")
                return True

            gallery_items = post.get("gallery_items")
            if isinstance(gallery_items, list):
                gallery_count = len(gallery_items)
            else:
                gallery_count = int(post.get("gallery_count", 0) or 0)
            has_deferred_album = isinstance(gallery_items, list) and any(
                isinstance(item, dict) and str(item.get("source") or "") in {"imgur_album", "external_album"}
                for item in gallery_items
            )
            min_items = max(2, int(getattr(self.config, "min_gallery_items", 2) or 2))
            max_items = max(2, int(getattr(self.config, "max_gallery_items", 10) or 10))
            if gallery_count < min_items and not has_deferred_album:
                if reason_list is not None:
                    reason_list.append(f"Too few gallery items: {gallery_count} < {min_items}")
                return True
            if gallery_count > max_items:
                post["gallery_count"] = max_items

        # Post age
        if not ignore_age and max_post_age_hours > 0:
            created_utc = post.get("created_utc", 0)
            if created_utc:
                post_time = datetime.fromtimestamp(created_utc, tz=timezone.utc)
                now = datetime.now(timezone.utc)
                age_hours = (now - post_time).total_seconds() / 3600

                if age_hours > max_post_age_hours:
                    if reason_list is not None:
                        reason_list.append(f"Too old: {age_hours:.1f}h > {max_post_age_hours}h")
                    return True

        # Title blacklist
        title = post.get("title", "").lower()
        for banned_word in self.config.title_blacklist:
            if banned_word.lower() in title:
                if reason_list is not None:
                    reason_list.append(f"Blacklisted word: '{banned_word}'")
                return True

        # Title whitelist (if set)
        if self.config.title_whitelist:
            found = False
            for allowed_word in self.config.title_whitelist:
                if allowed_word.lower() in title:
                    found = True
                    break

            if not found:
                if reason_list is not None:
                    reason_list.append("Not in whitelist")
                return True

        return False

    def filter_posts(
        self,
        posts: List[Dict[str, Any]],
        verbose: bool = False,
        *,
        ignore_age: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Filter list of posts based on all rules.

        Args:
            posts: List of post dictionaries
            verbose: If True, log why posts are filtered

        Returns:
            Filtered list of posts
        """
        filtered = []
        skipped_count = 0

        for post in posts:
            reasons = []

            if self.should_skip_post(post, reasons, ignore_age=ignore_age):
                skipped_count += 1
                if verbose and reasons:
                    log(f"Filtered r/{post.get('subreddit')}/{post.get('id')}: {', '.join(reasons)}")
                continue

            filtered.append(post)

        if skipped_count > 0:
            log(f"Filtered out {skipped_count} posts, {len(filtered)} remaining", "DEBUG")

        return filtered

    def should_force_video(self, img_streak: int) -> bool:
        """
        Check if we should force next post to be video.

        Args:
            img_streak: Current consecutive image count

        Returns:
            True if should force video
        """
        return img_streak >= self.config.max_images_in_row

    def should_avoid_subreddit(self, subreddit: str, recent_subreddits: List[str]) -> bool:
        """
        Check if we should avoid this subreddit to maintain variety.

        Args:
            subreddit: Subreddit name to check
            recent_subreddits: List of recently posted subreddit names

        Returns:
            True if should avoid this subreddit
        """
        if not recent_subreddits or self.config.avoid_duplicate_subreddit_streak <= 0:
            return False

        # Check last N subreddits
        recent = recent_subreddits[-self.config.avoid_duplicate_subreddit_streak :]

        # If all recent posts are from same subreddit, avoid it
        if all(sr == subreddit for sr in recent):
            return True

        return False

    def _log_normalized(self, value: Any, max_value: float) -> float:
        """Normalize skewed engagement counts onto a 0..1 scale."""
        try:
            number = max(0.0, float(value or 0))
        except (TypeError, ValueError):
            number = 0.0
        max_value = max(1.0, float(max_value or 1.0))
        return min(1.0, math.log1p(number) / math.log1p(max_value))

    def _freshness_score(self, post: Dict[str, Any]) -> float:
        """Score newer posts higher while respecting source max-age rules."""
        created_utc = post.get("created_utc", 0)
        if not created_utc:
            return 0.5

        try:
            post_time = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return 0.5

        age_hours = max(0.0, (datetime.now(timezone.utc) - post_time).total_seconds() / 3600)
        rule = self._effective_rule(post.get("subreddit", ""))
        max_age = int(rule.get("max_post_age_hours", self.config.max_post_age_hours) or 0)
        horizon = max(1.0, float(max_age or 72))
        return max(0.0, min(1.0, 1.0 - (age_hours / horizon)))

    def _media_type_score(
        self,
        post: Dict[str, Any],
        force_type: Optional[str],
        img_streak: int,
    ) -> float:
        """Score media type based on current posting mix."""
        media_type = post.get("type")
        if force_type:
            return 1.0 if media_type == force_type else 0.2

        if media_type == "video":
            max_images = int(getattr(self.config, "max_images_in_row", 0) or 0)
            if max_images > 0:
                streak_ratio = min(1.0, max(0.0, img_streak / max_images))
                return 0.65 + (0.35 * streak_ratio)
            return 0.7

        if media_type == "image":
            return 0.6

        if media_type == "gallery":
            return 0.65

        return 0.4

    def _title_quality_score(self, post: Dict[str, Any]) -> float:
        """Score titles that are readable, specific, and not engagement-bait."""
        title = str(post.get("title") or "").strip()
        if not title:
            return 0.2

        length = len(title)
        if 12 <= length <= 120:
            score = 1.0
        elif length < 12:
            score = max(0.25, length / 12.0)
        elif length <= 180:
            score = max(0.45, 1.0 - ((length - 120) / 120.0))
        else:
            score = 0.35

        lower_title = title.lower()
        weak_phrases = {
            "upvote",
            "please like",
            "subscribe",
            "link in bio",
            "follow me",
            "onlyfans",
            "click here",
        }
        if any(phrase in lower_title for phrase in weak_phrases):
            score *= 0.35

        letters = [char for char in title if char.isalpha()]
        if len(letters) >= 8:
            uppercase_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
            if uppercase_ratio > 0.7:
                score *= 0.55

        punctuation_ratio = sum(1 for char in title if char in string.punctuation) / max(1, length)
        if punctuation_ratio > 0.28:
            score *= 0.65

        return max(0.0, min(1.0, score))

    def _subreddit_repetition_score(
        self,
        post: Dict[str, Any],
        recent_subreddits: Optional[List[str]],
    ) -> float:
        """Score sources that have not been used recently higher."""
        if not recent_subreddits:
            return 1.0

        subreddit = str(post.get("subreddit") or "").strip().lower()
        recent = [str(value or "").strip().lower() for value in recent_subreddits if value]
        if not subreddit or not recent:
            return 1.0

        if recent[-1] == subreddit:
            return 0.0

        lookback = max(1, int(getattr(self.config, "avoid_duplicate_subreddit_streak", 3) or 3))
        recent_window = recent[-lookback:]
        if subreddit in recent_window:
            return 0.35

        if subreddit in recent[-10:]:
            return 0.70

        return 1.0

    def score_post(
        self,
        post: Dict[str, Any],
        *,
        max_upvotes: float = 1.0,
        max_comments: float = 1.0,
        force_type: Optional[str] = None,
        recent_subreddits: Optional[List[str]] = None,
        img_streak: int = 0,
    ) -> float:
        """Calculate a smart content score for a candidate post."""
        weights = getattr(self.config, "smart_scoring_weights", {}) or {}
        if not weights:
            weights = {
                "upvotes": 0.35,
                "comments": 0.20,
                "freshness": 0.20,
                "media_type": 0.10,
                "title_quality": 0.15,
                "subreddit_repetition": 0.20,
            }

        components = {
            "upvotes": self._log_normalized(post.get("upvotes", 0), max_upvotes),
            "comments": self._log_normalized(post.get("num_comments", 0), max_comments),
            "freshness": self._freshness_score(post),
            "media_type": self._media_type_score(post, force_type, img_streak),
            "title_quality": self._title_quality_score(post),
            "subreddit_repetition": self._subreddit_repetition_score(post, recent_subreddits),
        }

        total_weight = sum(max(0.0, float(weights.get(key, 0.0) or 0.0)) for key in components)
        if total_weight <= 0:
            base_score = 1.0
        else:
            weighted_sum = 0.0
            for key, value in components.items():
                weighted_sum += value * max(0.0, float(weights.get(key, 0.0) or 0.0))
            base_score = weighted_sum / total_weight

        source_weight = 1.0
        if hasattr(self.config, "get_subreddit_priority_weight"):
            source_weight = self.config.get_subreddit_priority_weight(post.get("subreddit", ""))

        return max(0.0, base_score * max(0.0, float(source_weight or 0.0)))

    def rank_posts(
        self,
        posts: List[Dict[str, Any]],
        *,
        force_type: Optional[str] = None,
        recent_subreddits: Optional[List[str]] = None,
        img_streak: int = 0,
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Return posts ordered by smart content score."""
        max_upvotes = max([float(post.get("upvotes", 0) or 0) for post in posts] + [1.0])
        max_comments = max([float(post.get("num_comments", 0) or 0) for post in posts] + [1.0])
        ranked = [
            (
                self.score_post(
                    post,
                    max_upvotes=max_upvotes,
                    max_comments=max_comments,
                    force_type=force_type,
                    recent_subreddits=recent_subreddits,
                    img_streak=img_streak,
                ),
                post,
            )
            for post in posts
        ]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked

    def pick_best_post(
        self,
        posts: List[Dict[str, Any]],
        force_type: Optional[str] = None,
        avoid_subreddit: Optional[str] = None,
        *,
        recent_subreddits: Optional[List[str]] = None,
        img_streak: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """
        Pick the best post from a list based on preferences.

        Args:
            posts: List of candidate posts
            force_type: Force 'image' or 'video' type
            avoid_subreddit: Avoid this subreddit if possible

        Returns:
            Best post or None
        """
        if not posts:
            return None

        # Filter by type if forced
        if force_type:
            typed_posts = [p for p in posts if p.get("type") == force_type]
            if typed_posts:
                posts = typed_posts
            else:
                log(f"No {force_type} posts available, using any type", "WARN")

        # Avoid specific subreddit if possible
        if avoid_subreddit:
            other_posts = [p for p in posts if p.get("subreddit") != avoid_subreddit]
            if other_posts:
                posts = other_posts

        if getattr(self.config, "smart_scoring_enabled", True):
            ranked = self.rank_posts(
                posts,
                force_type=force_type,
                recent_subreddits=recent_subreddits,
                img_streak=img_streak,
            )
            pool_size = max(1, int(getattr(self.config, "smart_scoring_top_pool_size", 8) or 8))
            top_pool = ranked[:pool_size]
            if any(score > 0 for score, _ in top_pool):
                weights = [max(0.0, score) for score, _ in top_pool]
            else:
                weights = [1.0 for _score, _post in top_pool]
            selected_score, selected_post = random.choices(top_pool, weights=weights, k=1)[0]
            selected_post["_score"] = round(selected_score, 4)
            return selected_post

        weights = []
        for post in posts:
            if hasattr(self.config, "get_subreddit_priority_weight"):
                weight = self.config.get_subreddit_priority_weight(post.get("subreddit", ""))
            else:
                weight = 1.0
            weights.append(max(0.0, float(weight or 0.0)))

        if any(weight > 0 for weight in weights):
            return random.choices(posts, weights=weights, k=1)[0]

        return random.choice(posts)
