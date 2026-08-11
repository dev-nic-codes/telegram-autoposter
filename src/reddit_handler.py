"""
Reddit API handler.
Fetches posts from Reddit and extracts media.
"""

import html
import re
import requests
import time
import random
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from typing import Dict, Any, Optional, List, Tuple, Set
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from utils import log, now_utc
from url_utils import path_for_domain


class RedditConfigurationError(requests.HTTPError):
    """Raised when Reddit API access is blocked by missing or invalid configuration."""


class RedditHandler:
    """Handles Reddit API operations"""

    def __init__(
        self,
        user_agent: str,
        *,
        reddit_client_id: str = "",
        reddit_client_secret: str = "",
        domain_downloaders_enabled: bool = True,
        imgur_album_downloads_enabled: bool = True,
        html_media_resolver_enabled: bool = True,
    ):
        self.user_agent = user_agent
        self.reddit_client_id = str(reddit_client_id or "").strip()
        self.reddit_client_secret = str(reddit_client_secret or "").strip()
        self.domain_downloaders_enabled = bool(domain_downloaders_enabled)
        self.imgur_album_downloads_enabled = bool(imgur_album_downloads_enabled)
        self.html_media_resolver_enabled = bool(html_media_resolver_enabled)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self._oauth_access_token: str = ""
        self._oauth_token_expires_at: Optional[datetime] = None
        self._rss_fallback_log_once: Set[str] = set()

    IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    VID_EXTS = (".mp4", ".webm", ".mov", ".gifv")
    IMGUR_PAGE_HOSTS = {"imgur.com", "m.imgur.com"}
    IMGUR_IMAGE_HOSTS = {"i.imgur.com"}
    VIDEO_PAGE_HOSTS = {
        "redgifs.com",
        "gfycat.com",
        "streamable.com",
        "clips.twitch.tv",
    }

    BLOCK_HOSTS = (
        "external-preview.redd.it",  # Often returns redirects
        "preview.redd.it",  # Low quality previews
    )

    def has_oauth_credentials(self) -> bool:
        """Return True when app-only OAuth credentials are configured."""
        return bool(self.reddit_client_id and self.reddit_client_secret)

    def uses_oauth(self) -> bool:
        """Return True when Reddit requests will use app-only OAuth."""
        return self.has_oauth_credentials()

    def _oauth_token_valid(self) -> bool:
        """Return True when the cached OAuth token is still usable."""
        return bool(
            self._oauth_access_token and self._oauth_token_expires_at and now_utc() < self._oauth_token_expires_at
        )

    def _describe_user_agent_hint(self) -> str:
        """Return a short descriptive user-agent example."""
        return "windows:telegram-autoposter:v2.0 (by /u/your_reddit_username)"

    def _response_text(self, response: Optional[requests.Response]) -> str:
        """Return safe lowercase response text for diagnostics."""
        if response is None:
            return ""
        try:
            return (response.text or "").lower()
        except Exception:
            return ""

    def _is_network_policy_block(self, response: Optional[requests.Response]) -> bool:
        """Return True when Reddit blocked non-authenticated traffic for network policy reasons."""
        if response is None or getattr(response, "status_code", None) != 403:
            return False
        content_type = str(response.headers.get("content-type", "") or "").lower()
        if "text/html" in content_type:
            return True
        body = self._response_text(response)
        markers = (
            "whoa there, pardner",
            "blocked due to a network policy",
            "developer credentials",
            "traffic not using oauth",
        )
        return any(marker in body for marker in markers)

    def _request_oauth_token(self, timeout: int = 25) -> str:
        """Fetch and cache an app-only Reddit OAuth token."""
        if not self.has_oauth_credentials():
            raise RedditConfigurationError(
                "Reddit OAuth credentials are missing. Set reddit_client_id and reddit_client_secret."
            )

        try:
            response = self.session.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(self.reddit_client_id, self.reddit_client_secret),
                data={"grant_type": "client_credentials"},
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            raise requests.HTTPError("Timeout retrieving Reddit OAuth token")
        except requests.exceptions.RequestException as e:
            raise requests.HTTPError(f"Error retrieving Reddit OAuth token: {e}")

        if response.status_code in {401, 403}:
            raise RedditConfigurationError(
                "Reddit OAuth token request was rejected. Verify reddit_client_id, "
                f"reddit_client_secret, and user_agent ({self._describe_user_agent_hint()}).",
                response=response,
            )
        if response.status_code == 429:
            raise requests.HTTPError("429 Too Many Requests: Reddit token rate limit", response=response)

        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError:
            raise requests.HTTPError("Reddit OAuth token response was not JSON", response=response)

        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise RedditConfigurationError(
                "Reddit OAuth token response did not include an access_token.",
                response=response,
            )

        expires_in = int(payload.get("expires_in", 3600) or 3600)
        refresh_margin = min(60, max(15, expires_in // 10))
        self._oauth_access_token = token
        self._oauth_token_expires_at = now_utc() + timedelta(seconds=max(30, expires_in - refresh_margin))
        log("Reddit OAuth token refreshed", "DEBUG")
        return token

    def _ensure_oauth_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid cached OAuth token, refreshing it when required."""
        if force_refresh or not self._oauth_token_valid():
            return self._request_oauth_token()
        return self._oauth_access_token

    def _decode_json_response(
        self,
        response: requests.Response,
        *,
        subreddit: str,
    ) -> Dict[str, Any]:
        """Decode a Reddit JSON response and raise a useful error when it is not JSON."""
        try:
            return response.json()
        except ValueError:
            snippet = (response.text or "").strip().replace("\n", " ")[:120]
            raise requests.HTTPError(
                f"Reddit returned a non-JSON response for r/{subreddit}: {snippet or 'empty body'}",
                response=response,
            )

    def _rss_feed_url(self, subreddit: str) -> str:
        """Return the Reddit Atom feed URL for a subreddit."""
        return f"https://www.reddit.com/r/{subreddit}/new.rss"

    def _rss_request_headers(self) -> Dict[str, str]:
        """Return headers tuned for Reddit Atom feeds."""
        return {
            "Accept": "application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.1",
        }

    def _parse_rss_timestamp(self, value: Any) -> float:
        """Convert an Atom timestamp to UTC seconds."""
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
        except ValueError:
            return 0.0

    def _rss_extract_urls(self, content_html: str) -> List[str]:
        """Extract raw href/src URLs from an Atom entry HTML payload."""
        if not content_html:
            return []

        urls: List[str] = []
        seen: Set[str] = set()
        patterns = [
            r'href="([^"]+)"',
            r"href='([^']+)'",
            r'src="([^"]+)"',
            r"src='([^']+)'",
        ]
        for pattern in patterns:
            for candidate in re.findall(pattern, content_html, flags=re.IGNORECASE):
                cleaned = self._clean_media_url(candidate)
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                urls.append(cleaned)
        return urls

    def _rss_pick_media(self, content_html: str) -> Optional[Dict[str, Any]]:
        """Select the best media candidate from a Reddit Atom entry."""
        urls = self._rss_extract_urls(content_html)
        preview_image: Optional[str] = None
        direct_image: Optional[str] = None
        video_manifest: Optional[str] = None

        for candidate in urls:
            host = self._host(candidate)
            lowered = candidate.lower().split("?", 1)[0]

            if host == "v.redd.it":
                base = self._clean_media_url(candidate).rstrip("/")
                if base:
                    video_manifest = f"{base}/DASHPlaylist.mpd"
                    break

            prepared = self._prepare_media_url(candidate, allow_preview=True)
            if not prepared:
                continue

            if host == "preview.redd.it" and not preview_image:
                preview_image = prepared

            if lowered.endswith(self.VID_EXTS):
                return {
                    "type": "video",
                    "url": prepared,
                    "post_hint": "hosted:video",
                    "media": {"reddit_video": {"fallback_url": prepared}},
                }

            if lowered.endswith(self.IMG_EXTS) and not direct_image:
                direct_image = prepared

        if video_manifest:
            return {
                "type": "video",
                "url": video_manifest,
                "post_hint": "hosted:video",
                "media": {"reddit_video": {"fallback_url": video_manifest}},
            }

        if direct_image:
            return {
                "type": "image",
                "url": direct_image,
                "post_hint": "image",
            }

        if preview_image:
            return {
                "type": "image",
                "url": preview_image,
                "post_hint": "image",
            }

        return None

    def _rss_entry_to_post(self, entry: Any, subreddit: str) -> Optional[Dict[str, Any]]:
        """Convert one Atom entry into a Reddit-like post payload."""
        ns = "{http://www.w3.org/2005/Atom}"
        title = str(entry.findtext(f"{ns}title", default="") or "").strip()
        author_name = str(entry.findtext(f"{ns}author/{ns}name", default="") or "").strip()
        entry_id = str(entry.findtext(f"{ns}id", default="") or "").strip()
        published = entry.findtext(f"{ns}published", default="") or entry.findtext(f"{ns}updated", default="")
        content_html = str(entry.findtext(f"{ns}content", default="") or "")

        link_href = ""
        for link in entry.findall(f"{ns}link"):
            href = str(link.get("href") or "").strip()
            rel = str(link.get("rel") or "alternate").strip().lower()
            if href and rel == "alternate":
                link_href = href
                break
            if href and not link_href:
                link_href = href

        media = self._rss_pick_media(content_html)
        if not media:
            return None

        normalized_subreddit = str(subreddit or "").strip().lower()
        for prefix in ("/r/", "r/"):
            if normalized_subreddit.startswith(prefix):
                normalized_subreddit = normalized_subreddit[len(prefix) :].strip()
        post_id = self._normalize_reddit_id(entry_id or link_href)
        if not post_id:
            return None

        permalink = path_for_domain(link_href, "reddit.com") or ""

        post: Dict[str, Any] = {
            "id": post_id,
            "name": f"t3_{post_id}",
            "subreddit": normalized_subreddit or subreddit,
            "title": title,
            "author": author_name or "unknown",
            "selftext": "",
            "url": media["url"],
            "url_overridden_by_dest": media["url"],
            "post_hint": media["post_hint"],
            "ups": 0,
            "num_comments": 0,
            "created_utc": self._parse_rss_timestamp(published),
            "over_18": False,
            "spoiler": False,
            "permalink": permalink,
            "domain": self._host(media["url"]),
        }

        if media["type"] == "video":
            post["media"] = media.get("media", {})
            post["secure_media"] = media.get("media", {})
        else:
            post["preview"] = {
                "images": [
                    {
                        "source": {
                            "url": media["url"],
                        }
                    }
                ]
            }

        return post

    def _fetch_subreddit_rss(
        self,
        subreddit: str,
        *,
        limit: int,
        timeout: int,
    ) -> Dict[str, Any]:
        """Fetch a subreddit via the public Atom feed when JSON is blocked."""
        try:
            response = self.session.get(
                self._rss_feed_url(subreddit),
                headers=self._rss_request_headers(),
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            raise requests.HTTPError(f"Timeout fetching r/{subreddit} RSS feed")
        except requests.exceptions.RequestException as e:
            raise requests.HTTPError(f"Error fetching r/{subreddit} RSS feed: {e}")

        if response.status_code == 429:
            raise requests.HTTPError("429 Too Many Requests: Reddit RSS rate limit", response=response)
        response.raise_for_status()

        try:
            root = ET.fromstring(response.text or "")
        except (ET.ParseError, DefusedXmlException) as e:
            raise requests.HTTPError(
                f"Reddit RSS parse error for r/{subreddit}: {e}",
                response=response,
            )

        ns = "{http://www.w3.org/2005/Atom}"
        children: List[Dict[str, Any]] = []
        for entry in root.findall(f"{ns}entry"):
            post = self._rss_entry_to_post(entry, subreddit)
            if not post:
                continue
            children.append({"kind": "t3", "data": post})
            if len(children) >= min(limit, 100):
                break

        if subreddit not in self._rss_fallback_log_once:
            self._rss_fallback_log_once.add(subreddit)
            log(f"Reddit JSON blocked for r/{subreddit}; using RSS media fallback", "INFO")

        return {
            "data": {
                "children": children,
                "after": None,
                "dist": len(children),
            }
        }

    def fetch_subreddit_new(
        self,
        subreddit: str,
        limit: int = 30,
        timeout: int = 25,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetch new posts from subreddit.

        Args:
            subreddit: Subreddit name (without r/)
            limit: Number of posts to fetch (max 100)
            timeout: Request timeout in seconds
            after: Reddit listing cursor for pagination

        Returns:
            Reddit API JSON response

        Raises:
            requests.HTTPError: If request fails
        """
        params = {
            "limit": min(limit, 100),
            "raw_json": 1,
        }
        if after:
            params["after"] = after

        log(
            f"Fetching r/{subreddit} (limit={limit}{f', after={after}' if after else ''})",
            "DEBUG",
        )

        try:
            if self.uses_oauth():
                url = f"https://oauth.reddit.com/r/{subreddit}/new"
                token = self._ensure_oauth_token()
                headers = {"Authorization": f"bearer {token}"}
                r = self.session.get(url, params=params, headers=headers, timeout=timeout)
                if r.status_code == 401:
                    token = self._ensure_oauth_token(force_refresh=True)
                    headers = {"Authorization": f"bearer {token}"}
                    r = self.session.get(url, params=params, headers=headers, timeout=timeout)
                if r.status_code in {401, 403} and self._is_network_policy_block(r):
                    raise RedditConfigurationError(
                        "Reddit blocked API access even after OAuth authentication. "
                        "Verify the Reddit app credentials and user_agent.",
                        response=r,
                    )
            else:
                url = f"https://www.reddit.com/r/{subreddit}/new.json"
                r = self.session.get(url, params=params, timeout=timeout)
                if r.status_code == 403 and self._is_network_policy_block(r):
                    return self._fetch_subreddit_rss(
                        subreddit,
                        limit=params["limit"],
                        timeout=timeout,
                    )
                if r.status_code == 429 and not after:
                    try:
                        return self._fetch_subreddit_rss(
                            subreddit,
                            limit=params["limit"],
                            timeout=timeout,
                        )
                    except requests.HTTPError:
                        raise requests.HTTPError(
                            "429 Too Many Requests: Reddit rate limit",
                            response=r,
                        )

            if r.status_code == 401:
                raise RedditConfigurationError(
                    "Reddit OAuth authentication failed. Check reddit_client_id, "
                    f"reddit_client_secret, and user_agent ({self._describe_user_agent_hint()}).",
                    response=r,
                )
            if r.status_code == 403:
                raise requests.HTTPError("403 Forbidden: Reddit blocked request", response=r)
            if r.status_code == 429:
                raise requests.HTTPError("429 Too Many Requests: Reddit rate limit", response=r)

            r.raise_for_status()
            return self._decode_json_response(r, subreddit=subreddit)

        except requests.exceptions.Timeout:
            raise requests.HTTPError(f"Timeout fetching r/{subreddit}")
        except RedditConfigurationError:
            raise
        except requests.exceptions.HTTPError as e:
            raise requests.HTTPError(
                f"Error fetching r/{subreddit}: {e}",
                response=getattr(e, "response", None),
            )
        except requests.exceptions.RequestException as e:
            raise requests.HTTPError(f"Error fetching r/{subreddit}: {e}")

    def _url_blocked(self, url: str) -> bool:
        """Check if URL is from a blocked host"""
        if not url:
            return True

        url_lower = url.lower()
        return any(blocked in url_lower for blocked in self.BLOCK_HOSTS)

    def _endswith_any(self, url: str, extensions: Tuple[str, ...]) -> bool:
        """Check if URL ends with any of the given extensions"""
        if not url:
            return False

        url_clean = url.lower().split("?")[0]  # Remove query params
        return url_clean.endswith(extensions)

    def _clean_media_url(self, url: Optional[str]) -> str:
        """Decode common Reddit URL escaping."""
        if not url:
            return ""
        return html.unescape(str(url).strip())

    def _host(self, url: str) -> str:
        """Return a normalized host without leading www."""
        try:
            host = urlsplit(self._clean_media_url(url)).netloc.lower()
        except ValueError:
            return ""
        if host.startswith("www."):
            host = host[4:]
        return host

    def _path_parts(self, url: str) -> List[str]:
        """Return cleaned URL path parts."""
        try:
            path = urlsplit(self._clean_media_url(url)).path or ""
        except ValueError:
            return []
        return [part for part in path.split("/") if part]

    def _is_imgur_album_url(self, url: str) -> bool:
        """Return True for Imgur album/gallery page URLs."""
        if self._host(url) not in self.IMGUR_PAGE_HOSTS:
            return False
        parts = self._path_parts(url)
        return len(parts) >= 2 and parts[0].lower() in {"a", "gallery"}

    def _imgur_media_id(self, url: str) -> str:
        """Extract a single-image Imgur ID from direct or page URLs."""
        if self._host(url) not in self.IMGUR_PAGE_HOSTS | self.IMGUR_IMAGE_HOSTS:
            return ""
        if self._is_imgur_album_url(url):
            return ""
        parts = self._path_parts(url)
        if not parts:
            return ""
        media_id = parts[-1].split(".", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9]{5,12}", media_id or ""):
            return ""
        return media_id

    def _imgur_single_direct_url(self, url: str) -> str:
        """Convert a single Imgur page URL to a direct i.imgur.com URL."""
        cleaned = self._clean_media_url(url)
        media_id = self._imgur_media_id(cleaned)
        if not media_id:
            return cleaned

        path_lower = (self._path_parts(cleaned)[-1] or "").lower()
        if path_lower.endswith(".gifv"):
            return f"https://i.imgur.com/{media_id}.mp4"
        if path_lower.endswith(self.VID_EXTS):
            extension = path_lower.rsplit(".", 1)[1]
            if extension == "gifv":
                extension = "mp4"
            return f"https://i.imgur.com/{media_id}.{extension}"
        if path_lower.endswith(self.IMG_EXTS):
            extension = path_lower.rsplit(".", 1)[1]
            return f"https://i.imgur.com/{media_id}.{extension}"
        return f"https://i.imgur.com/{media_id}.jpg"

    def _normalize_imgur_url_parts(self, host: str, path: str) -> Optional[str]:
        """Normalize Imgur page/direct URL variants to a single signature."""
        if host not in self.IMGUR_PAGE_HOSTS | self.IMGUR_IMAGE_HOSTS:
            return None

        parts = [part for part in path.split("/") if part]
        if not parts:
            return None

        if host in self.IMGUR_PAGE_HOSTS and len(parts) >= 2 and parts[0].lower() in {"a", "gallery"}:
            album_id = re.sub(r"[^A-Za-z0-9_-]+", "", parts[1])
            return f"imgur.com/{parts[0].lower()}/{album_id.lower()}" if album_id else None

        media_id = parts[-1].split(".", 1)[0]
        if re.fullmatch(r"[A-Za-z0-9]{5,12}", media_id or ""):
            return f"imgur.com/{media_id.lower()}"
        return None

    def _normalize_url(self, url: str) -> str:
        """Normalize media URLs so transient query params do not bypass dedupe."""
        if not url:
            return ""
        value = self._clean_media_url(url)
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
        path = re.sub(r"/+", "/", path).rstrip("/")

        if host in {"preview.redd.it", "external-preview.redd.it"}:
            host = "i.redd.it"
        imgur_signature = self._normalize_imgur_url_parts(host, path)
        if imgur_signature:
            return imgur_signature
        if host == "v.redd.it":
            parts = [part for part in path.split("/") if part]
            path = f"/{parts[0].lower()}" if parts else ""
        elif host in {"i.redd.it", "redd.it"}:
            path = path.lower()

        return f"{host}{path}"

    def _gallery_item_urls(self, gallery_items: Any) -> List[str]:
        """Return clean media URLs from a gallery item list."""
        if not isinstance(gallery_items, list):
            return []

        urls: List[str] = []
        seen: Set[str] = set()
        for item in gallery_items:
            if isinstance(item, dict):
                url = self._clean_media_url(item.get("url"))
            else:
                url = self._clean_media_url(item)
            if not url:
                continue
            normalized = self._normalize_url(url)
            if normalized and normalized in seen:
                continue
            if normalized:
                seen.add(normalized)
            urls.append(url)
        return urls

    def _normalize_title_signature(self, title: str) -> str:
        """Normalize titles so simple casing/punctuation changes do not bypass dedupe."""
        if not title:
            return ""
        normalized = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", title.lower())).strip()
        if len(normalized) < 24:
            return ""
        return normalized

    def _normalize_permalink_signature(self, permalink: str) -> str:
        """Normalize Reddit permalinks into a stable path-only signature."""
        if not permalink:
            return ""
        value = str(permalink).strip()
        reddit_path = path_for_domain(value, "reddit.com", allow_bare_host=True)
        if reddit_path is not None:
            value = reddit_path
        return value.split("?", 1)[0].strip().rstrip("/")

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

    def _build_post_signatures(
        self,
        *,
        post_id: str = "",
        url: str = "",
        title: str = "",
        permalink: str = "",
        crosspost_parent: str = "",
        gallery_items: Optional[List[Dict[str, Any]]] = None,
    ) -> Set[str]:
        """Build stable signatures for content-level dedupe."""
        signatures: Set[str] = set()
        normalized_url = self._normalize_url(url)
        normalized_title = self._normalize_title_signature(title)
        normalized_permalink = self._normalize_permalink_signature(permalink)
        normalized_crosspost = str(crosspost_parent or "").strip().lower()
        normalized_post_id = self._normalize_reddit_id(post_id)
        permalink_post_id = self._normalize_reddit_id(normalized_permalink)
        crosspost_post_id = self._normalize_reddit_id(normalized_crosspost)

        if normalized_url:
            signatures.add(f"url:{normalized_url}")
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
        for gallery_url in self._gallery_item_urls(gallery_items):
            normalized_gallery_url = self._normalize_url(gallery_url)
            if not normalized_gallery_url or normalized_gallery_url == normalized_url:
                continue
            signatures.add(f"url:{normalized_gallery_url}")
            if normalized_title:
                signatures.add(f"title_url:{normalized_title}|{normalized_gallery_url}")
        if normalized_post_id:
            signatures.add(f"id:{normalized_post_id}")
            signatures.add(f"fullname:t3_{normalized_post_id}")
        if permalink_post_id:
            signatures.add(f"id:{permalink_post_id}")

        return signatures

    def _preview_to_direct_url(self, url: str) -> str:
        """Convert preview.redd.it image URLs to direct i.redd.it URLs when possible."""
        cleaned = self._clean_media_url(url)
        if "preview.redd.it/" in cleaned:
            base = cleaned.split("?", 1)[0]
            return base.replace("://preview.redd.it/", "://i.redd.it/")
        return cleaned

    def _prepare_media_url(self, url: Optional[str], *, allow_preview: bool = False) -> Optional[str]:
        """Normalize and validate a candidate media URL."""
        cleaned = self._clean_media_url(url)
        if not cleaned:
            return None

        direct = self._preview_to_direct_url(cleaned)
        if direct and not self._url_blocked(direct):
            return direct

        if not self._url_blocked(cleaned):
            return cleaned

        if allow_preview and "preview.redd.it/" in cleaned:
            return cleaned

        return None

    def _extract_reddit_video_url(self, post: Dict[str, Any]) -> Optional[str]:
        """Extract Reddit-hosted video URL from post"""
        if not isinstance(post, dict):
            return None

        # Safely get media objects
        media = post.get("media") or {}
        secure_media = post.get("secure_media") or {}
        preview = post.get("preview") or {}

        # Check different possible locations for reddit_video
        sources = [
            media.get("reddit_video"),
            secure_media.get("reddit_video"),
            preview.get("reddit_video_preview"),
        ]

        def sanitize(url: Optional[str]) -> Optional[str]:
            """
            Normalize malformed Reddit URLs that sometimes contain multiple '?'.
            Example: ...?source=fallback?source=fallback -> ...?source=fallback&source=fallback
            """
            if not url or "?" not in url:
                return url
            parts = url.split("?")
            if len(parts) <= 2:
                return url
            base = parts[0]
            query = "&".join(p for p in parts[1:] if p)
            return f"{base}?{query}" if query else base

        for source in sources:
            if isinstance(source, dict):
                fallback = sanitize(source.get("fallback_url"))
                if fallback and not self._url_blocked(fallback):
                    log(f"Found Reddit video URL: {fallback[:80]}", "DEBUG")
                    return fallback

                # Try dash_url as alternative
                dash = sanitize(source.get("dash_url"))
                if dash and not self._url_blocked(dash):
                    log(f"Found Reddit DASH URL: {dash[:80]}", "DEBUG")
                    return dash

        return None

    def _extract_gallery_media(self, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract a Reddit gallery as ordered image items, with a single-media fallback."""
        gallery = post.get("gallery_data") or {}
        metadata = post.get("media_metadata") or {}
        items = gallery.get("items") or []

        if not isinstance(metadata, dict) or not isinstance(items, list):
            return None

        gallery_items: List[Dict[str, Any]] = []
        fallback_media: Optional[Dict[str, str]] = None

        for item in items:
            if not isinstance(item, dict):
                continue

            media_id = item.get("media_id")
            media = metadata.get(media_id) or {}
            if not isinstance(media, dict):
                continue
            if media.get("status") != "valid":
                continue

            media_kind = str(media.get("e") or "")
            source = media.get("s") or {}
            previews = media.get("p") or []
            candidates: List[str] = []
            width = None
            height = None

            if isinstance(source, dict):
                width = source.get("x")
                height = source.get("y")
                if media_kind == "AnimatedImage":
                    candidates.extend([source.get("mp4"), source.get("gif"), source.get("u")])
                else:
                    candidates.extend([source.get("u"), source.get("gif"), source.get("mp4")])

            if isinstance(previews, list):
                for preview in reversed(previews):
                    if isinstance(preview, dict):
                        candidates.append(preview.get("u"))

            for candidate in candidates:
                prepared = self._prepare_media_url(candidate, allow_preview=True)
                if not prepared:
                    continue
                if self._endswith_any(prepared, self.VID_EXTS):
                    if fallback_media is None:
                        fallback_media = {"type": "video", "url": prepared}
                    break
                if self._endswith_any(prepared, self.IMG_EXTS):
                    if media_kind != "AnimatedImage" and not self._endswith_any(prepared, (".gif",)):
                        gallery_items.append(
                            {
                                "url": prepared,
                                "width": width,
                                "height": height,
                                "media_id": str(media_id or ""),
                            }
                        )
                    if fallback_media is None:
                        fallback_media = {"type": "image", "url": prepared}
                    break

        if len(gallery_items) >= 2:
            return {
                "type": "gallery",
                "url": gallery_items[0]["url"],
                "gallery_items": gallery_items,
                "gallery_count": len(gallery_items),
            }

        return fallback_media

    def _extract_imgur_media(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract single-image, gifv, and album targets from Imgur links."""
        if not self.domain_downloaders_enabled:
            return None

        prepared = self._prepare_media_url(url)
        if not prepared:
            return None

        host = self._host(prepared)
        if host not in self.IMGUR_PAGE_HOSTS | self.IMGUR_IMAGE_HOSTS:
            return None

        if self._is_imgur_album_url(prepared):
            if not self.imgur_album_downloads_enabled:
                return None
            return {
                "type": "gallery",
                "url": prepared,
                "gallery_items": [
                    {
                        "url": prepared,
                        "source": "imgur_album",
                    }
                ],
                "gallery_count": 0,
            }

        direct = self._imgur_single_direct_url(prepared)
        if direct.lower().split("?", 1)[0].endswith((".mp4", ".webm", ".mov")):
            return {"type": "video", "url": direct}
        if direct.lower().split("?", 1)[0].endswith(self.IMG_EXTS):
            return {"type": "image", "url": direct}

        return None

    def _extract_hosted_page_media(self, post: Dict[str, Any], url: str) -> Optional[Dict[str, str]]:
        """Accept known hosted media pages so MediaHandler can resolve direct media."""
        if not (self.domain_downloaders_enabled and self.html_media_resolver_enabled):
            return None

        prepared = self._prepare_media_url(url)
        if not prepared:
            return None

        host = self._host(prepared)
        post_hint = str(post.get("post_hint") or "").strip().lower()
        domain = str(post.get("domain") or "").strip().lower()
        if domain.startswith("www."):
            domain = domain[4:]

        if (
            post_hint in {"hosted:video", "rich:video"}
            or host in self.VIDEO_PAGE_HOSTS
            or domain in self.VIDEO_PAGE_HOSTS
        ):
            return {"type": "video", "url": prepared}

        if post_hint == "image":
            return {"type": "image", "url": prepared}

        return None

    def extract_media(self, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract media URL and type from Reddit post.

        Args:
            post: Reddit post data dictionary

        Returns:
            Dict with 'type' and 'url', or None if no media found
        """
        # Check crossposts first
        crosspost_parent = post.get("crosspost_parent_list")
        if isinstance(crosspost_parent, list) and crosspost_parent:
            crosspost_media = self.extract_media(crosspost_parent[0])
            if crosspost_media:
                return crosspost_media

        # Check for Reddit-hosted video
        video_url = self._extract_reddit_video_url(post)
        if video_url:
            return {"type": "video", "url": video_url}

        # Check Reddit galleries
        gallery_media = self._extract_gallery_media(post)
        if gallery_media:
            return gallery_media

        # Check post URL
        raw_url = post.get("url_overridden_by_dest") or post.get("url") or ""
        imgur_media = self._extract_imgur_media(raw_url)
        if imgur_media:
            return imgur_media

        url = self._prepare_media_url(raw_url)

        if url:
            if url.lower().split("?", 1)[0].endswith(".gifv"):
                return {"type": "video", "url": url}

            if self._endswith_any(url, self.VID_EXTS):
                return {"type": "video", "url": url}

            if self._endswith_any(url, self.IMG_EXTS):
                return {"type": "image", "url": url}

            hosted_media = self._extract_hosted_page_media(post, url)
            if hosted_media:
                return hosted_media

        # Check preview images
        preview = post.get("preview")
        if isinstance(preview, dict):
            images = preview.get("images")
            if images and isinstance(images, list):
                source = images[0].get("source", {})
                preview_url = self._prepare_media_url(source.get("url"), allow_preview=True)

                if preview_url:
                    return {"type": "image", "url": preview_url}

        return None

    def parse_posts(
        self,
        reddit_data: Dict[str, Any],
        seen_ids: Set[str],
        seen_urls: Optional[Set[str]] = None,
        seen_signatures: Optional[Set[str]] = None,
        block_crossposts: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Parse Reddit API response into list of candidate posts.

        Args:
            reddit_data: Reddit API JSON response
            seen_ids: Set of already-seen post IDs
            seen_urls: Set of already-seen media URLs (normalized)
            seen_signatures: Set of already-seen content signatures

        Returns:
            List of post dictionaries with id, subreddit, title, type, url, etc.
        """
        posts = []
        seen_urls = seen_urls or set()
        seen_signatures = seen_signatures or set()
        batch_signatures: Set[str] = set()

        children = reddit_data.get("data", {}).get("children", [])

        for child in children:
            post = child.get("data", {})

            post_id = post.get("id")
            if not post_id or post_id in seen_ids:
                continue

            # Skip stickied posts
            if post.get("stickied"):
                continue

            # Extract media
            media = self.extract_media(post)
            if not media:
                continue

            media_urls = [media.get("url", "")] + self._gallery_item_urls(media.get("gallery_items"))
            media_url_norms = {self._normalize_url(str(media_url or "")) for media_url in media_urls if media_url}
            media_url_norms.discard("")
            if media_url_norms & seen_urls:
                continue

            crosspost_parent = post.get("crosspost_parent", "")
            crosspost_parent_list = post.get("crosspost_parent_list")
            if not crosspost_parent and isinstance(crosspost_parent_list, list) and crosspost_parent_list:
                parent = crosspost_parent_list[0]
                if isinstance(parent, dict):
                    crosspost_parent = parent.get("name") or parent.get("id") or ""
            post_signatures = self._build_post_signatures(
                post_id=post_id,
                url=media.get("url", ""),
                title=post.get("title", ""),
                permalink=post.get("permalink", ""),
                crosspost_parent=crosspost_parent if block_crossposts else "",
                gallery_items=media.get("gallery_items"),
            )

            if post_signatures & seen_signatures:
                continue

            if post_signatures & batch_signatures:
                continue

            batch_signatures.update(post_signatures)

            # Get post metadata
            parsed_post = {
                "id": post_id,
                "subreddit": post.get("subreddit", "unknown"),
                "title": post.get("title", ""),
                "author": post.get("author", ""),
                "selftext": post.get("selftext", ""),
                "type": media["type"],
                "url": media["url"],
                "upvotes": post.get("ups", 0),
                "num_comments": post.get("num_comments", 0),
                "created_utc": post.get("created_utc", 0),
                "nsfw": post.get("over_18", False),
                "spoiler": post.get("spoiler", False),
                "permalink": post.get("permalink", ""),
                "crosspost_parent": crosspost_parent,
            }
            if media.get("gallery_items"):
                parsed_post["gallery_items"] = media["gallery_items"]
                parsed_post["gallery_count"] = media.get(
                    "gallery_count",
                    len(media["gallery_items"]),
                )
            posts.append(parsed_post)

        return posts

    def calculate_post_age_hours(self, created_utc: float) -> float:
        """Calculate post age in hours"""
        if not created_utc:
            return 0

        post_time = datetime.fromtimestamp(created_utc, tz=timezone.utc)
        age = now_utc() - post_time
        return age.total_seconds() / 3600

    def fetch_with_jitter(
        self, subreddit: str, limit: int = 30, jitter_range: Tuple[float, float] = (0.8, 1.8)
    ) -> Dict[str, Any]:
        """
        Fetch subreddit with random jitter delay.

        Args:
            subreddit: Subreddit name
            limit: Number of posts
            jitter_range: Min and max seconds for random sleep

        Returns:
            Reddit API response
        """
        result = self.fetch_subreddit_new(subreddit, limit)

        # Sleep random time to avoid hammering
        sleep_time = random.uniform(*jitter_range)
        time.sleep(sleep_time)

        return result
