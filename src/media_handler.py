"""
Media download and processing handler.
Downloads and validates media files from URLs.
"""

import os
import html
import json
import re
import requests
import subprocess
import tempfile
from html.parser import HTMLParser
from io import BytesIO
from typing import Optional, Tuple, Dict, Any, List
from urllib.parse import urlsplit
from PIL import Image, ImageOps
from utils import log, format_file_size


class _MediaMetaParser(HTMLParser):
    """Collect media-related tags from a hosted media page."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: List[Tuple[str, str]] = []
        self.sources: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = {str(key).lower(): value for key, value in attrs if value}
        tag_lower = tag.lower()

        if tag_lower == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or attr_map.get("itemprop") or "").strip().lower()
            content = (attr_map.get("content") or "").strip()
            if key and content:
                self.meta.append((key, content))
            return

        if tag_lower == "link":
            rel = (attr_map.get("rel") or "").strip().lower()
            href = (attr_map.get("href") or "").strip()
            if rel and href:
                self.meta.append((rel, href))
            return

        if tag_lower in {"source", "video"}:
            src = (attr_map.get("src") or "").strip()
            if src:
                self.sources.append(src)


class MediaHandler:
    """Handles media downloading and processing"""

    IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    VID_EXTS = (".mp4", ".webm", ".mov")
    IMGUR_PAGE_HOSTS = {"imgur.com", "m.imgur.com"}
    IMGUR_IMAGE_HOSTS = {"i.imgur.com"}

    def __init__(
        self,
        user_agent: str,
        max_size_mb: int = 45,
        *,
        domain_downloaders_enabled: bool = True,
        imgur_album_downloads_enabled: bool = True,
        html_media_resolver_enabled: bool = True,
    ):
        self.user_agent = user_agent
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.domain_downloaders_enabled = bool(domain_downloaders_enabled)
        self.imgur_album_downloads_enabled = bool(imgur_album_downloads_enabled)
        self.html_media_resolver_enabled = bool(html_media_resolver_enabled)
        self.has_ffmpeg = self._check_ffmpeg()
        self.has_ffprobe = self._check_ffprobe()

    def _check_ffmpeg(self) -> bool:
        """Check if ffmpeg is available"""
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                log("ffmpeg available for audio processing", "DEBUG")
                return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pass

        log("ffmpeg not found - videos may not have audio", "WARN")
        return False

    def _check_ffprobe(self) -> bool:
        """Check if ffprobe is available for video metadata."""
        try:
            result = subprocess.run(
                ["ffprobe", "-version"],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                log("ffprobe available for video rule checks", "DEBUG")
                return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pass

        log("ffprobe not found - video duration/audio/orientation checks are limited", "WARN")
        return False

    def _clean_url(self, url: str) -> str:
        """Decode common HTML/JSON URL escaping from hosted media pages."""
        value = html.unescape(str(url or "").strip()).strip("\"'")
        return value.replace("\\/", "/")

    def _host(self, url: str) -> str:
        """Return a normalized URL host without a leading www."""
        try:
            host = urlsplit(self._clean_url(url)).netloc.lower()
        except ValueError:
            return ""
        if host.startswith("www."):
            host = host[4:]
        return host

    def _path_parts(self, url: str) -> List[str]:
        """Return cleaned path parts for URL routing."""
        try:
            path = urlsplit(self._clean_url(url)).path or ""
        except ValueError:
            return []
        return [part for part in path.split("/") if part]

    def _extension_type(self, url: str) -> Optional[str]:
        """Infer image/video from a direct media URL extension."""
        clean_path = self._clean_url(url).split("?", 1)[0].split("#", 1)[0].lower()
        if clean_path.endswith(".gifv"):
            return "video"
        if clean_path.endswith(self.VID_EXTS):
            return "video"
        if clean_path.endswith(self.IMG_EXTS):
            return "image"
        return None

    def _convert_gifv_url(self, url: str) -> str:
        """Normalize .gifv pages to the MP4 file variant used by most hosts."""
        cleaned = self._clean_url(url)
        base, suffix = (cleaned.split("?", 1) + [""])[:2] if "?" in cleaned else (cleaned, "")
        if not base.lower().endswith(".gifv"):
            return cleaned
        converted = base[:-5] + ".mp4"
        return f"{converted}?{suffix}" if suffix else converted

    def _is_imgur_album_url(self, url: str) -> bool:
        """Return True for imgur album/gallery pages."""
        if self._host(url) not in self.IMGUR_PAGE_HOSTS:
            return False
        parts = self._path_parts(url)
        return len(parts) >= 2 and parts[0].lower() in {"a", "gallery"}

    def _imgur_single_direct_url(self, url: str) -> str:
        """Convert a single Imgur page URL to a direct i.imgur.com media URL when safe."""
        cleaned = self._clean_url(url)
        host = self._host(cleaned)
        if host not in self.IMGUR_PAGE_HOSTS | self.IMGUR_IMAGE_HOSTS:
            return cleaned
        if self._is_imgur_album_url(cleaned):
            return cleaned

        parts = self._path_parts(cleaned)
        if not parts:
            return cleaned

        image_id = parts[-1].split(".", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9]{5,12}", image_id or ""):
            return cleaned

        path_lower = parts[-1].lower()
        if path_lower.endswith(".gifv"):
            return f"https://i.imgur.com/{image_id}.mp4"
        if path_lower.endswith(self.VID_EXTS):
            extension = path_lower.rsplit(".", 1)[1]
            return f"https://i.imgur.com/{image_id}.{extension}"
        if path_lower.endswith(self.IMG_EXTS):
            extension = path_lower.rsplit(".", 1)[1]
            return f"https://i.imgur.com/{image_id}.{extension}"

        return f"https://i.imgur.com/{image_id}.jpg"

    def _resolve_known_direct_url(self, url: str, expected_type: str) -> str:
        """Apply cheap direct URL normalizations before downloading."""
        cleaned = self._clean_url(url)
        if not self.domain_downloaders_enabled:
            return cleaned

        if cleaned.lower().split("?", 1)[0].endswith(".gifv"):
            return self._convert_gifv_url(cleaned)

        host = self._host(cleaned)
        if host in self.IMGUR_PAGE_HOSTS | self.IMGUR_IMAGE_HOSTS:
            return self._imgur_single_direct_url(cleaned)

        return cleaned

    def _read_limited_text_response(
        self,
        response: requests.Response,
        max_bytes: int = 3 * 1024 * 1024,
    ) -> str:
        """Read a bounded HTML response body for media URL extraction."""
        chunks: List[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                remaining = max(0, max_bytes - (total - len(chunk)))
                if remaining:
                    chunks.append(chunk[:remaining])
                break
            chunks.append(chunk)

        raw = b"".join(chunks)
        encoding = response.encoding or "utf-8"
        return raw.decode(encoding, errors="ignore")

    def _extract_html_media_candidates(
        self,
        html_text: str,
        expected_type: str = "media",
    ) -> List[Tuple[str, str]]:
        """Extract direct media candidates from OpenGraph tags and embedded URLs."""
        parser = _MediaMetaParser()
        try:
            parser.feed(html_text or "")
        except Exception as e:
            log(f"HTML media parser warning: {e}", "WARN")

        video_keys = {
            "og:video",
            "og:video:url",
            "og:video:secure_url",
            "twitter:player:stream",
            "twitter:player:stream:url",
        }
        image_keys = {
            "og:image",
            "og:image:url",
            "og:image:secure_url",
            "twitter:image",
            "twitter:image:src",
            "image",
            "image_src",
        }
        candidates: List[Tuple[str, str]] = []

        def add_candidate(raw_url: str, hinted_type: Optional[str] = None) -> None:
            candidate = self._clean_url(raw_url)
            if not candidate.startswith(("http://", "https://")):
                return
            candidate = self._resolve_known_direct_url(candidate, hinted_type or expected_type)
            inferred_type = self._extension_type(candidate) or hinted_type or "media"
            if expected_type in {"image", "video"} and inferred_type not in {expected_type, "media"}:
                return
            candidates.append((candidate, inferred_type if inferred_type in {"image", "video"} else expected_type))

        for key, value in parser.meta:
            key_lower = key.lower()
            if key_lower in video_keys:
                add_candidate(value, "video")
            elif key_lower in image_keys:
                add_candidate(value, "image")

        for value in parser.sources:
            add_candidate(value, self._extension_type(value))

        search_text = (html_text or "").replace("\\/", "/")
        url_pattern = re.compile(
            r"https?://[^\"'<>\\\s]+?\.(?:jpg|jpeg|png|webp|gif|gifv|mp4|webm|mov)(?:\?[^\"'<>\\\s]*)?",
            re.IGNORECASE,
        )
        for match in url_pattern.finditer(search_text):
            add_candidate(match.group(0), self._extension_type(match.group(0)))

        unique: List[Tuple[str, str]] = []
        seen = set()
        for url, media_type in candidates:
            key = url.split("?", 1)[0].lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append((url, media_type))

        return unique

    def _download_from_html_page(
        self,
        page_url: str,
        html_text: str,
        expected_type: str,
        timeout: int,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Resolve a hosted media page to a direct media URL and download it."""
        if not (
            self.domain_downloaders_enabled
            and self.html_media_resolver_enabled
            and expected_type in {"image", "video", "media"}
        ):
            return False, None, "Hosted-page media resolver is disabled"

        candidates = self._extract_html_media_candidates(html_text, expected_type)
        for candidate_url, candidate_type in candidates[:12]:
            success, data, error = self.download(
                candidate_url,
                candidate_type if candidate_type in {"image", "video"} else expected_type,
                timeout=timeout,
                allow_page_resolve=False,
            )
            if success and data:
                log(
                    f"Resolved hosted media page to {candidate_type}: {candidate_url[:100]}",
                    "DEBUG",
                )
                return True, data, ""
            if error:
                log(f"Resolved media candidate failed: {error}", "WARN")

        return False, None, f"No direct media candidate found in hosted page: {page_url[:100]}"

    def _extract_imgur_album_items(
        self,
        album_url: str,
        *,
        max_items: int,
        timeout: int,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Scrape direct image URLs from a public Imgur album/gallery page."""
        if not (
            self.domain_downloaders_enabled
            and self.imgur_album_downloads_enabled
            and self._is_imgur_album_url(album_url)
        ):
            return [], "Imgur album downloads are disabled"

        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
        try:
            with requests.get(
                album_url,
                headers=headers,
                stream=True,
                timeout=timeout,
                allow_redirects=True,
            ) as response:
                response.raise_for_status()
                html_text = self._read_limited_text_response(response, max_bytes=5 * 1024 * 1024)
        except requests.exceptions.RequestException as e:
            return [], f"Imgur album page fetch failed: {e}"
        except Exception as e:
            return [], f"Imgur album parse failed: {e}"

        candidates = [
            url
            for url, media_type in self._extract_html_media_candidates(html_text, "image")
            if media_type == "image" and self._host(url) in self.IMGUR_IMAGE_HOSTS
        ]

        def imgur_sort_key(value: str) -> tuple[int, str]:
            stem = os.path.basename(urlsplit(value).path).split(".", 1)[0]
            is_thumbnail = len(stem) > 6 and stem[-1:].lower() in {"s", "b", "t", "m", "l", "h"}
            return (1 if is_thumbnail else 0, value)

        unique_urls: List[str] = []
        seen = set()
        for candidate in sorted(candidates, key=imgur_sort_key):
            key = candidate.split("?", 1)[0].lower()
            if key in seen:
                continue
            seen.add(key)
            unique_urls.append(candidate)
            if len(unique_urls) >= max_items:
                break

        if not unique_urls:
            return [], "No direct Imgur images found in album page"

        return (
            [
                {
                    "type": "photo",
                    "url": item_url,
                    "source": "imgur_album",
                    "index": index,
                }
                for index, item_url in enumerate(unique_urls, 1)
            ],
            "",
        )

    def download(
        self,
        url: str,
        expected_type: str = "media",
        timeout: int = 60,
        allow_page_resolve: bool = True,
    ) -> Tuple[bool, Optional[bytes], str]:
        """
        Download media from URL with size limits.

        Args:
            url: URL to download from
            expected_type: 'image' or 'video' (for validation)
            timeout: Request timeout in seconds

        Returns:
            Tuple of (success, bytes_data, error_message)
        """
        url = self._resolve_known_direct_url(url, expected_type)
        headers = {"User-Agent": self.user_agent}

        log(f"Downloading {expected_type}: {url[:100]}...", "DEBUG")

        try:
            with requests.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
                r.raise_for_status()

                # Check Content-Type
                content_type = (r.headers.get("Content-Type") or "").lower()

                # Resolve hosted media pages (Imgur pages, Redgifs/Streamable embeds, etc.).
                if expected_type in {"image", "video", "media"} and "text/html" in content_type:
                    if allow_page_resolve:
                        html_text = self._read_limited_text_response(r)
                        success, data, error = self._download_from_html_page(
                            url,
                            html_text,
                            expected_type,
                            timeout,
                        )
                        if success:
                            return True, data, ""
                        return False, None, f"Expected {expected_type} but got HTML: {error}"
                    return False, None, f"Expected {expected_type} but got Content-Type: {content_type}"

                # Check Content-Length if available
                content_length = r.headers.get("Content-Length")
                if content_length:
                    try:
                        size = int(content_length)
                        if size > self.max_size_bytes:
                            return (
                                False,
                                None,
                                f"File too large: {format_file_size(size)} (max: {format_file_size(self.max_size_bytes)})",
                            )
                    except (ValueError, TypeError):
                        pass

                # Download with streaming
                buffer = BytesIO()
                total_bytes = 0

                for chunk in r.iter_content(chunk_size=65536):  # 64KB chunks
                    if not chunk:
                        continue

                    total_bytes += len(chunk)

                    if total_bytes > self.max_size_bytes:
                        return False, None, f"File exceeds {format_file_size(self.max_size_bytes)} during download"

                    buffer.write(chunk)

                data = buffer.getvalue()

                if not data:
                    return False, None, "Downloaded file is empty"

                log(
                    f"Downloaded successfully: {format_file_size(len(data))}, Content-Type: {content_type or 'unknown'}",
                    "DEBUG",
                )
                return True, data, ""

        except requests.exceptions.Timeout:
            return False, None, f"Download timeout after {timeout}s"

        except requests.exceptions.RequestException as e:
            return False, None, f"Download failed: {str(e)}"

        except Exception as e:
            return False, None, f"Unexpected error during download: {str(e)}"

    def download_gallery(
        self,
        gallery_items: Any,
        *,
        max_items: int = 10,
        min_items: int = 2,
        timeout: int = 60,
    ) -> Tuple[bool, Optional[List[Dict[str, Any]]], str]:
        """
        Download image items for a Telegram media group.

        Returns:
            Tuple of (success, media_items, error_message). Each media item contains
            bytes plus the source URL and is ready for TelegramHandler.send_media_group.
        """
        if not isinstance(gallery_items, list):
            return False, None, "Gallery has no item list"

        max_items = max(2, min(10, int(max_items or 10)))
        min_items = max(2, min(max_items, int(min_items or 2)))
        expanded_items: List[Any] = []
        expansion_errors: List[str] = []
        for item in gallery_items:
            item_url = str(item.get("url") or "").strip() if isinstance(item, dict) else str(item or "").strip()
            item_source = str(item.get("source") or "") if isinstance(item, dict) else ""
            if item_url and (item_source == "imgur_album" or self._is_imgur_album_url(item_url)):
                album_items, album_error = self._extract_imgur_album_items(
                    item_url,
                    max_items=max_items,
                    timeout=timeout,
                )
                if album_items:
                    expanded_items.extend(album_items)
                    continue
                if album_error:
                    expansion_errors.append(album_error)
            expanded_items.append(item)

        gallery_items = expanded_items
        downloaded: List[Dict[str, Any]] = []
        errors: List[str] = list(expansion_errors)
        seen_urls = set()

        for index, item in enumerate(gallery_items, 1):
            if len(downloaded) >= max_items:
                break

            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
            else:
                url = str(item or "").strip()

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            success, data, error = self.download(url, "image", timeout=timeout)
            if not success or not data:
                errors.append(f"{index}: {error or 'download failed'}")
                log(f"Gallery item {index} download failed: {error}", "WARN")
                continue

            downloaded.append(
                {
                    "type": "photo",
                    "bytes": data,
                    "url": url,
                    "index": index,
                }
            )

        if len(downloaded) < min_items:
            detail = "; ".join(errors[:3]) if errors else "not enough usable gallery items"
            return (
                False,
                None,
                f"Only {len(downloaded)} gallery item(s) downloaded; need {min_items}. {detail}",
            )

        log(f"Downloaded gallery media group: {len(downloaded)} item(s)", "DEBUG")
        return True, downloaded, ""

    def download_reddit_video_with_audio(self, video_url: str) -> Tuple[bool, Optional[bytes], str]:
        """
        Download Reddit video and merge with audio track.
        Reddit stores video and audio in separate files.

        Args:
            video_url: Reddit video URL (usually ends with DASH_xxx.mp4)

        Returns:
            Tuple of (success, bytes_data, error_message)
        """
        if not self.has_ffmpeg:
            log("ffmpeg not available, downloading video without audio merge", "WARN")
            return self.download(video_url, "video")

        # Normalize away query params so URL parsing works for CMAF/DASH variants.
        video_url_clean = (video_url or "").split("?", 1)[0]

        log("Detected Reddit video, attempting audio merge...")

        try:
            # Preserve query params (e.g., ?source=fallback) for derived URLs too.
            query_suffix = ""
            if "?" in (video_url or ""):
                query_suffix = "?" + video_url.split("?", 1)[1]

            # Extract base URL across current Reddit variants.
            base_url: Optional[str] = None
            if "/DASH_" in video_url_clean:
                base_url = video_url_clean.rsplit("/DASH_", 1)[0]
            elif "/CMAF_" in video_url_clean:
                base_url = video_url_clean.rsplit("/CMAF_", 1)[0]
            elif "/HLSPlaylist.m3u8" in video_url_clean or "/DASHPlaylist.mpd" in video_url_clean:
                base_url = video_url_clean.rsplit("/", 1)[0]
            elif "v.redd.it" in video_url_clean and video_url_clean.endswith(".mp4"):
                # Generic v.redd.it mp4 path.
                base_url = video_url_clean.rsplit("/", 1)[0]

            if not base_url:
                log("Could not derive Reddit video base URL, downloading normally", "WARN")
                return self.download(video_url, "video")

            # Try manifest-based download first. This often includes both audio and video.
            manifest_candidates = []
            if "/DASHPlaylist.mpd" in video_url_clean or "/HLSPlaylist.m3u8" in video_url_clean:
                manifest_candidates.append(video_url)
            manifest_candidates.extend(
                [
                    f"{base_url}/DASHPlaylist.mpd{query_suffix}",
                    f"{base_url}/HLSPlaylist.m3u8{query_suffix}",
                ]
            )

            for manifest_url in manifest_candidates:
                log(f"Attempting manifest download via ffmpeg: {manifest_url[:80]}...")
                ok_m, manifest_bytes, err_m = self._download_with_ffmpeg(manifest_url)
                if ok_m and manifest_bytes:
                    log(f"Manifest download succeeded: {format_file_size(len(manifest_bytes))}", "SUCCESS")
                    return True, manifest_bytes, ""
                if err_m:
                    log(f"Manifest download failed: {err_m}", "WARN")

            # Download video track
            log("Downloading video track...")
            success_v, video_bytes, error_v = self.download(video_url, "video")
            if not success_v:
                return False, None, f"Video download failed: {error_v}"

            log(f"Video track downloaded: {format_file_size(len(video_bytes))}")

            # Try multiple known Reddit audio track variants.
            audio_candidates = [
                f"{base_url}/DASH_audio.mp4{query_suffix}",
                f"{base_url}/DASH_AUDIO_128.mp4{query_suffix}",
                f"{base_url}/DASH_AUDIO_64.mp4{query_suffix}",
                f"{base_url}/CMAF_audio.mp4{query_suffix}",
                f"{base_url}/audio.mp4{query_suffix}",
            ]

            audio_bytes: Optional[bytes] = None
            last_audio_error = ""
            for audio_url in audio_candidates:
                log(f"Attempting audio track: {audio_url[:80]}...")
                success_a, candidate_bytes, error_a = self.download(audio_url, "video")
                if success_a and candidate_bytes:
                    audio_bytes = candidate_bytes
                    log(f"Audio track downloaded: {format_file_size(len(audio_bytes))}")
                    break
                last_audio_error = error_a

            if not audio_bytes:
                log(f"No audio track available, using video only ({last_audio_error})", "WARN")
                return True, video_bytes, ""

            # Merge video and audio using ffmpeg
            merged_bytes = self._merge_video_audio(video_bytes, audio_bytes)

            if merged_bytes:
                log(f"Successfully merged video with audio: {format_file_size(len(merged_bytes))}", "SUCCESS")
                return True, merged_bytes, ""
            else:
                log("Merge failed, returning video without audio", "WARN")
                return True, video_bytes, ""

        except Exception as e:
            log(f"Error during Reddit video processing: {e}", "ERROR")
            # Fallback to simple download
            return self.download(video_url, "video")

    def _download_with_ffmpeg(self, source_url: str) -> Tuple[bool, Optional[bytes], str]:
        """
        Use ffmpeg to download/mux a remote source (e.g., DASH/HLS manifest) into an MP4.
        This can capture both audio and video when direct audio URLs are blocked.
        """
        temp_dir = tempfile.gettempdir()
        output_file = os.path.join(temp_dir, f"ffmpeg_dl_{os.getpid()}.mp4")

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                source_url,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                output_file,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            if result.returncode != 0:
                error_msg = result.stderr.decode("utf-8", errors="ignore")
                return False, None, f"ffmpeg return code {result.returncode}: {error_msg[:200]}"

            if not os.path.exists(output_file):
                return False, None, "ffmpeg did not create output file"

            with open(output_file, "rb") as f:
                data = f.read()

            if not data:
                return False, None, "ffmpeg output file is empty"

            return True, data, ""

        except subprocess.TimeoutExpired:
            return False, None, "ffmpeg download timeout"
        except Exception as e:
            return False, None, f"ffmpeg download error: {e}"
        finally:
            try:
                if os.path.exists(output_file):
                    os.remove(output_file)
            except Exception as e:
                log(f"Could not delete temp file {output_file}: {e}", "WARN")

    def _merge_video_audio(self, video_bytes: bytes, audio_bytes: bytes) -> Optional[bytes]:
        """
        Merge video and audio tracks using ffmpeg.

        Args:
            video_bytes: Video file bytes
            audio_bytes: Audio file bytes

        Returns:
            Merged video bytes or None if failed
        """
        temp_dir = tempfile.gettempdir()
        video_file = os.path.join(temp_dir, f"video_{os.getpid()}.mp4")
        audio_file = os.path.join(temp_dir, f"audio_{os.getpid()}.mp4")
        output_file = os.path.join(temp_dir, f"output_{os.getpid()}.mp4")

        try:
            # Write temp files
            with open(video_file, "wb") as f:
                f.write(video_bytes)
            with open(audio_file, "wb") as f:
                f.write(audio_bytes)

            log("Merging video and audio with ffmpeg...")

            # Run ffmpeg to merge
            cmd = [
                "ffmpeg",
                "-i",
                video_file,
                "-i",
                audio_file,
                "-c:v",
                "copy",  # Copy video without re-encoding
                "-c:a",
                "aac",  # Encode audio as AAC
                "-b:a",
                "128k",  # Audio bitrate
                "-shortest",  # Match shortest stream
                "-y",  # Overwrite output
                output_file,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            if result.returncode != 0:
                error_msg = result.stderr.decode("utf-8", errors="ignore")
                log(f"ffmpeg error: {error_msg[:200]}", "ERROR")
                return None

            # Check if output file was created
            if not os.path.exists(output_file):
                log("ffmpeg did not create output file", "ERROR")
                return None

            # Read merged file
            with open(output_file, "rb") as f:
                merged_bytes = f.read()

            if not merged_bytes:
                log("Output file is empty", "ERROR")
                return None

            return merged_bytes

        except subprocess.TimeoutExpired:
            log("ffmpeg merge timeout", "ERROR")
            return None

        except Exception as e:
            log(f"Error in ffmpeg merge: {e}", "ERROR")
            return None

        finally:
            # Cleanup temp files
            for f in [video_file, audio_file, output_file]:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except Exception as e:
                    log(f"Could not delete temp file {f}: {e}", "WARN")

    def _write_temp_bytes(self, data: bytes, suffix: str = ".mp4") -> str:
        """Write bytes to a temporary file and return its path."""
        fd, path = tempfile.mkstemp(prefix=f"autoposter_{os.getpid()}_", suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        return path

    def _probe_video_file(self, input_file: str, size_bytes: int = 0) -> Dict[str, Any]:
        """Inspect a local video file with ffprobe."""
        info: Dict[str, Any] = {
            "size_bytes": size_bytes,
            "size_human": format_file_size(size_bytes),
            "duration_seconds": None,
            "width": None,
            "height": None,
            "orientation": "unknown",
            "has_audio": None,
            "format_name": "",
            "video_codec": "",
            "audio_codecs": [],
            "probe_ok": False,
            "error": "",
        }

        if not self.has_ffprobe:
            info["error"] = "ffprobe not available"
            return info

        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            input_file,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="ignore")
                info["error"] = f"ffprobe return code {result.returncode}: {error[:160]}"
                return info

            payload = json.loads(result.stdout.decode("utf-8", errors="ignore") or "{}")
            streams = payload.get("streams", []) if isinstance(payload, dict) else []
            fmt = payload.get("format", {}) if isinstance(payload, dict) else {}
            video_stream = next(
                (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
                {},
            )
            audio_streams = [
                stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"
            ]

            if not video_stream:
                info["error"] = "No video stream found"
                return info

            def to_float(value: Any) -> Optional[float]:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return None
                return number if number >= 0 else None

            def to_int(value: Any) -> Optional[int]:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    return None
                return number if number > 0 else None

            duration = to_float(video_stream.get("duration"))
            if duration is None:
                duration = to_float(fmt.get("duration"))

            width = to_int(video_stream.get("width"))
            height = to_int(video_stream.get("height"))
            rotation = 0
            tags = video_stream.get("tags")
            if isinstance(tags, dict):
                try:
                    rotation = int(float(tags.get("rotate", 0) or 0)) % 360
                except (TypeError, ValueError):
                    rotation = 0
            if rotation in {90, 270} and width and height:
                width, height = height, width

            if width and height:
                ratio = width / max(1, height)
                if 0.90 <= ratio <= 1.10:
                    orientation = "square"
                elif width > height:
                    orientation = "landscape"
                else:
                    orientation = "portrait"
            else:
                orientation = "unknown"

            info.update(
                {
                    "duration_seconds": duration,
                    "width": width,
                    "height": height,
                    "orientation": orientation,
                    "has_audio": bool(audio_streams),
                    "format_name": str(fmt.get("format_name") or ""),
                    "video_codec": str(video_stream.get("codec_name") or ""),
                    "audio_codecs": [
                        str(stream.get("codec_name") or "") for stream in audio_streams if stream.get("codec_name")
                    ],
                    "probe_ok": True,
                    "error": "",
                }
            )
            return info
        except subprocess.TimeoutExpired:
            info["error"] = "ffprobe timeout"
            return info
        except Exception as e:
            info["error"] = f"ffprobe error: {e}"
            return info

    def validate_video_info(
        self,
        info: Dict[str, Any],
        *,
        max_duration_seconds: int = 0,
        audio_policy: str = "allow_silent",
        orientation_rule: str = "any",
    ) -> Tuple[bool, str]:
        """Validate probed video metadata against configured rules."""
        audio_policy = str(audio_policy or "allow_silent").strip().lower()
        orientation_rule = str(orientation_rule or "any").strip().lower()
        probe_ok = bool(info.get("probe_ok"))

        if self.has_ffprobe and not probe_ok:
            return False, str(info.get("error") or "Could not inspect video")

        if max_duration_seconds and max_duration_seconds > 0:
            duration = info.get("duration_seconds")
            if duration is None:
                return False, "Could not determine video duration"
            if float(duration) > float(max_duration_seconds):
                return False, f"Video too long: {float(duration):.1f}s > {max_duration_seconds}s"

        if audio_policy == "require_audio":
            if not probe_ok or info.get("has_audio") is None:
                return False, "Could not determine whether video has audio"
            if not info.get("has_audio"):
                return False, "Video has no audio"

        if orientation_rule != "any":
            orientation = str(info.get("orientation") or "unknown")
            if orientation == "unknown":
                return False, "Could not determine video orientation"
            if orientation != orientation_rule:
                return False, f"Video orientation is {orientation}, requires {orientation_rule}"

        return True, ""

    def _ffmpeg_video_process(
        self,
        data: bytes,
        args: List[str],
        *,
        timeout: int = 180,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Run ffmpeg against a byte payload and return MP4 output bytes."""
        if not self.has_ffmpeg:
            return False, None, "ffmpeg is not available"

        input_file = ""
        output_file = ""
        try:
            input_file = self._write_temp_bytes(data, ".input")
            fd, output_file = tempfile.mkstemp(
                prefix=f"autoposter_out_{os.getpid()}_",
                suffix=".mp4",
            )
            os.close(fd)

            cmd = ["ffmpeg", "-y", "-i", input_file] + args + [output_file]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="ignore")
                return False, None, f"ffmpeg return code {result.returncode}: {error[:200]}"
            if not os.path.exists(output_file):
                return False, None, "ffmpeg did not create output file"

            with open(output_file, "rb") as handle:
                output = handle.read()
            if not output:
                return False, None, "ffmpeg output file is empty"
            return True, output, ""
        except subprocess.TimeoutExpired:
            return False, None, "ffmpeg video processing timeout"
        except Exception as e:
            return False, None, f"ffmpeg video processing error: {e}"
        finally:
            for path in (input_file, output_file):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception as e:
                    log(f"Could not delete temp file {path}: {e}", "WARN")

    def _remux_video_to_mp4(self, data: bytes, info: Dict[str, Any]) -> Tuple[bool, Optional[bytes], str]:
        """Remux a video to MP4 with faststart, falling back to transcoding if copy fails."""
        args = ["-map", "0:v:0"]
        if info.get("has_audio"):
            args.extend(["-map", "0:a?", "-c:a", "copy"])
        else:
            args.append("-an")
        args.extend(["-c:v", "copy", "-movflags", "+faststart"])

        success, output, error = self._ffmpeg_video_process(data, args, timeout=180)
        if success and output:
            return True, output, ""

        log(f"Video remux failed, trying transcode: {error}", "WARN")
        target_mb = max(1, min(45, int(self.max_size_bytes / (1024 * 1024))))
        return self._transcode_video_to_target(data, info, target_mb * 1024 * 1024)

    def _transcode_video_to_target(
        self,
        data: bytes,
        info: Dict[str, Any],
        target_bytes: int,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Transcode a video to H.264/AAC MP4 near a target size."""
        duration = info.get("duration_seconds")
        try:
            duration_seconds = max(1.0, float(duration or 0.0))
        except (TypeError, ValueError):
            duration_seconds = 60.0

        has_audio = bool(info.get("has_audio"))
        target_bits = max(1, int(target_bytes * 8 * 0.90))
        total_kbps = max(350, int(target_bits / duration_seconds / 1000))
        audio_kbps = 96 if has_audio else 0
        video_kbps = max(250, total_kbps - audio_kbps)

        args = [
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{max(video_kbps * 2, 500)}k",
        ]
        if has_audio:
            args.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", f"{audio_kbps}k"])
        else:
            args.append("-an")
        args.extend(["-movflags", "+faststart"])

        return self._ffmpeg_video_process(data, args, timeout=240)

    def prepare_video(
        self,
        data: bytes,
        *,
        max_duration_seconds: int = 0,
        audio_policy: str = "allow_silent",
        orientation_rule: str = "any",
        convert_to_mp4: bool = True,
        compression_enabled: bool = True,
        compression_target_mb: int = 40,
    ) -> Tuple[bool, Optional[bytes], str, Dict[str, Any]]:
        """Validate, convert, and optionally compress a video payload."""
        if not data:
            info = self.get_video_info(data)
            return False, None, "Downloaded video is empty", info

        info = self.get_video_info(data)
        valid, reason = self.validate_video_info(
            info,
            max_duration_seconds=max_duration_seconds,
            audio_policy=audio_policy,
            orientation_rule=orientation_rule,
        )
        if not valid:
            return False, None, reason, info

        if audio_policy == "prefer_audio" and info.get("has_audio") is False:
            log("Video has no audio; allowing because audio policy is prefer_audio", "WARN")

        processed = data
        target_bytes = max(1, int(compression_target_mb or 40)) * 1024 * 1024
        should_compress = bool(compression_enabled and len(processed) > target_bytes)

        if should_compress:
            if not self.has_ffmpeg:
                return False, None, "ffmpeg is required for video compression", info
            success, converted, error = self._transcode_video_to_target(processed, info, target_bytes)
            if not success or not converted:
                return False, None, f"Video compression failed: {error}", info
            processed = converted
            if len(processed) > target_bytes:
                return (
                    False,
                    None,
                    (
                        f"Compressed video is still too large: "
                        f"{format_file_size(len(processed))} > {format_file_size(target_bytes)}"
                    ),
                    self.get_video_info(processed),
                )
        elif convert_to_mp4:
            if self.has_ffmpeg:
                success, converted, error = self._remux_video_to_mp4(processed, info)
                if success and converted:
                    processed = converted
                else:
                    log(f"Video conversion failed; using original: {error}", "WARN")
            else:
                log("ffmpeg not available; video conversion skipped", "WARN")

        if len(processed) > self.max_size_bytes:
            return (
                False,
                None,
                (
                    f"Video too large after processing: "
                    f"{format_file_size(len(processed))} (max: {format_file_size(self.max_size_bytes)})"
                ),
                self.get_video_info(processed),
            )

        final_info = self.get_video_info(processed)
        valid, reason = self.validate_video_info(
            final_info,
            max_duration_seconds=max_duration_seconds,
            audio_policy=audio_policy,
            orientation_rule=orientation_rule,
        )
        if not valid:
            return False, None, reason, final_info

        return True, processed, "", final_info

    def compress_video_for_retry(
        self,
        data: bytes,
        target_mb: int = 30,
    ) -> Tuple[bool, Optional[bytes], str, Dict[str, Any]]:
        """Compress a video payload for a retry after Telegram rejects an upload."""
        info = self.get_video_info(data)
        if not data:
            return False, None, "Video payload is empty", info
        if not self.has_ffmpeg:
            return False, None, "ffmpeg is not available", info

        target_bytes = min(
            self.max_size_bytes,
            max(1, int(target_mb or 30)) * 1024 * 1024,
        )
        success, output, error = self._transcode_video_to_target(data, info, target_bytes)
        if not success or not output:
            return False, None, error or "Video compression failed", info
        if len(output) > self.max_size_bytes:
            return (
                False,
                None,
                (
                    f"Compressed video is still too large: "
                    f"{format_file_size(len(output))} > {format_file_size(self.max_size_bytes)}"
                ),
                self.get_video_info(output),
            )
        if len(output) >= len(data):
            return False, None, "Compressed video is not smaller than the original", info

        return True, output, "", self.get_video_info(output)

    def _image_to_retry_jpeg(self, img: Image.Image, quality: int) -> bytes:
        """Encode a PIL image as a retry-safe JPEG."""
        output = BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
        return output.getvalue()

    def compress_image_for_retry(
        self,
        data: bytes,
        target_mb: int = 8,
        max_dimension: int = 2560,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Compress an image payload for a retry after Telegram rejects an upload."""
        if not data:
            return False, None, "Image payload is empty"

        target_bytes = max(1, min(int(target_mb or 8), 10)) * 1024 * 1024
        try:
            with Image.open(BytesIO(data)) as img:
                normalized = ImageOps.exif_transpose(img)
                if normalized.mode in {"RGBA", "LA"} or (normalized.mode == "P" and "transparency" in normalized.info):
                    rgba = normalized.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    background.alpha_composite(rgba)
                    working = background.convert("RGB")
                else:
                    working = normalized.convert("RGB")

                resampling = getattr(Image, "Resampling", Image)
                max_dimension = max(512, int(max_dimension or 2560))
                if max(working.size) > max_dimension:
                    working.thumbnail(
                        (max_dimension, max_dimension),
                        getattr(resampling, "LANCZOS", Image.BICUBIC),
                    )

                best: Optional[bytes] = None
                for quality in (88, 82, 76, 70, 64, 58):
                    candidate = self._image_to_retry_jpeg(working, quality)
                    if best is None or len(candidate) < len(best):
                        best = candidate
                    if len(candidate) <= target_bytes:
                        return True, candidate, ""

                if best and len(best) < len(data):
                    return True, best, ""
                return False, None, "Compressed image is not smaller than the original"
        except Exception as e:
            return False, None, f"Image compression failed: {e}"

    def validate_image(self, data: bytes) -> Tuple[bool, str, Optional[Tuple[int, int]]]:
        """
        Validate image data and get dimensions.

        Args:
            data: Image bytes

        Returns:
            Tuple of (is_valid, error_message, dimensions)
        """
        if not data:
            return False, "No data", None

        try:
            img = Image.open(BytesIO(data))
            width, height = img.size

            log(f"Image validated: {width}x{height}, format: {img.format}", "DEBUG")
            return True, "", (width, height)

        except Exception as e:
            return False, f"Invalid image: {str(e)}", None

    def get_video_info(self, data: bytes) -> Dict[str, Any]:
        """
        Get video information using ffprobe when available.

        Args:
            data: Video bytes

        Returns:
            Dict with size, duration, dimensions, orientation, and audio fields.
        """
        size_bytes = len(data or b"")
        if not data:
            return {
                "size_bytes": 0,
                "size_human": format_file_size(0),
                "duration_seconds": None,
                "width": None,
                "height": None,
                "orientation": "unknown",
                "has_audio": None,
                "format_name": "",
                "video_codec": "",
                "audio_codecs": [],
                "probe_ok": False,
                "error": "No video data",
            }

        input_file = ""
        try:
            input_file = self._write_temp_bytes(data, ".mp4")
            return self._probe_video_file(input_file, size_bytes)
        except Exception as e:
            return {
                "size_bytes": size_bytes,
                "size_human": format_file_size(size_bytes),
                "duration_seconds": None,
                "width": None,
                "height": None,
                "orientation": "unknown",
                "has_audio": None,
                "format_name": "",
                "video_codec": "",
                "audio_codecs": [],
                "probe_ok": False,
                "error": f"Video probe failed: {e}",
            }
        finally:
            try:
                if input_file and os.path.exists(input_file):
                    os.remove(input_file)
            except Exception as e:
                log(f"Could not delete temp file {input_file}: {e}", "WARN")

    def _analyze_image_quality(self, data: bytes) -> Dict[str, Any]:
        """Return lightweight image metrics used by quality filters."""
        result: Dict[str, Any] = {
            "blur_score": 0.0,
            "edge_density": 0.0,
            "screenshot_like": False,
        }

        try:
            with Image.open(BytesIO(data)) as img:
                normalized = ImageOps.exif_transpose(img)
                width, height = normalized.size
                aspect_ratio = width / max(1, height)

                gray = ImageOps.grayscale(normalized)
                resampling = getattr(Image, "Resampling", Image)
                gray.thumbnail((256, 256), getattr(resampling, "LANCZOS", Image.BICUBIC))
                sample_width, sample_height = gray.size
                pixels = list(gray.getdata())
        except Exception as e:
            result["error"] = str(e)
            return result

        if sample_width < 3 or sample_height < 3:
            return result

        lap_sum = 0.0
        lap_sq_sum = 0.0
        edge_count = 0
        sample_count = 0

        for y in range(1, sample_height - 1):
            row = y * sample_width
            for x in range(1, sample_width - 1):
                idx = row + x
                lap = (
                    int(pixels[idx]) * 4
                    - int(pixels[idx - 1])
                    - int(pixels[idx + 1])
                    - int(pixels[idx - sample_width])
                    - int(pixels[idx + sample_width])
                )
                lap_abs = abs(lap)
                lap_sum += lap
                lap_sq_sum += lap * lap
                edge_count += 1 if lap_abs >= 18 else 0
                sample_count += 1

        if sample_count <= 0:
            return result

        lap_mean = lap_sum / sample_count
        blur_score = max(0.0, (lap_sq_sum / sample_count) - (lap_mean * lap_mean))
        edge_density = edge_count / sample_count

        common_ratios = (
            16 / 9,
            9 / 16,
            19.5 / 9,
            9 / 19.5,
            20 / 9,
            9 / 20,
            4 / 3,
            3 / 4,
            1.0,
        )
        ratio_match = any(abs(aspect_ratio - target) / target <= 0.035 for target in common_ratios)
        screenshot_like = (
            ratio_match and max(width, height) >= 1000 and min(width, height) >= 500 and edge_density >= 0.055
        )

        result.update(
            {
                "blur_score": blur_score,
                "edge_density": edge_density,
                "screenshot_like": screenshot_like,
            }
        )
        return result

    def is_image_quality_acceptable(
        self,
        data: bytes,
        min_width: int = 800,
        *,
        enabled: bool = True,
        min_height: int = 0,
        aspect_ratio_min: float = 0.20,
        aspect_ratio_max: float = 5.00,
        blur_filter_enabled: bool = False,
        blur_score_min: float = 35.0,
        screenshot_filter_enabled: bool = False,
        text_heavy_filter_enabled: bool = False,
        text_heavy_max_edge_density: float = 0.18,
    ) -> Tuple[bool, str]:
        """
        Check if image meets minimum quality standards.

        Args:
            data: Image bytes
            min_width: Minimum width in pixels

        Returns:
            Tuple of (acceptable, reason)
        """
        valid, error, dimensions = self.validate_image(data)

        if not valid:
            return False, error

        if not dimensions:
            return False, "Could not determine dimensions"

        if not enabled:
            return True, ""

        width, height = dimensions

        min_width = max(0, int(min_width or 0))
        min_height = max(0, int(min_height or 0))
        if min_width and width < min_width:
            return False, f"Image too small: {width}px wide (minimum: {min_width}px)"
        if min_height and height < min_height:
            return False, f"Image too small: {height}px tall (minimum: {min_height}px)"

        aspect_ratio = width / height if height > 0 else 0

        try:
            ratio_min = float(aspect_ratio_min)
            ratio_max = float(aspect_ratio_max)
        except (TypeError, ValueError):
            ratio_min, ratio_max = 0.20, 5.00
        if ratio_max < ratio_min:
            ratio_min, ratio_max = ratio_max, ratio_min

        if ratio_min > 0 and aspect_ratio < ratio_min:
            return False, f"Image aspect ratio too narrow: {aspect_ratio:.2f} (minimum: {ratio_min:.2f})"
        if ratio_max > 0 and aspect_ratio > ratio_max:
            return False, f"Image aspect ratio too wide: {aspect_ratio:.2f} (maximum: {ratio_max:.2f})"

        needs_analysis = bool(blur_filter_enabled or screenshot_filter_enabled or text_heavy_filter_enabled)
        if needs_analysis:
            analysis = self._analyze_image_quality(data)
            if blur_filter_enabled:
                blur_score = float(analysis.get("blur_score") or 0.0)
                if blur_score < float(blur_score_min or 0.0):
                    return (
                        False,
                        f"Image appears blurry: score {blur_score:.1f} (minimum: {float(blur_score_min or 0.0):.1f})",
                    )
            if screenshot_filter_enabled and bool(analysis.get("screenshot_like")):
                return False, "Image looks like a screenshot"
            if text_heavy_filter_enabled:
                edge_density = float(analysis.get("edge_density") or 0.0)
                max_density = float(text_heavy_max_edge_density or 0.18)
                if edge_density > max_density:
                    return (
                        False,
                        f"Image appears text-heavy: edge density {edge_density:.3f} (maximum: {max_density:.3f})",
                    )

        return True, ""

    def looks_like_image(self, data: bytes) -> bool:
        """Quick check if bytes start with image header"""
        if not data or len(data) < 16:
            return False

        # Check magic bytes
        if data.startswith(b"\xff\xd8\xff"):  # JPEG
            return True
        if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
            return True
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":  # WEBP
            return True
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):  # GIF
            return True

        return False
