"""
Telegram API handler.
All Telegram-related API calls and operations.
"""

import requests
import json
import time
from typing import Dict, Any, Optional, List
from io import BytesIO
from utils import log


class TelegramAPIError(RuntimeError):
    """Structured Telegram API error without exposing request credentials."""

    def __init__(self, method: str, data: Dict[str, Any]):
        self.method = method
        self.error_code = data.get("error_code")
        self.description = str(data.get("description") or "Unknown Telegram API error")
        parameters = data.get("parameters")
        self.retry_after = None
        if isinstance(parameters, dict):
            try:
                self.retry_after = max(0, int(parameters.get("retry_after")))
            except (TypeError, ValueError):
                pass

        code_text = f" {self.error_code}" if self.error_code is not None else ""
        super().__init__(f"Telegram API error on {method}:{code_text} {self.description}")


class TelegramHandler:
    """Handles all Telegram API operations"""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self._last_update_conflict_recovery = 0.0
        self._get_updates_failure_count = 0
        self.get_updates_backoff_seconds = 0.0

    def _safe_exception_text(self, error: Exception) -> str:
        """Return exception text with the bot token and tokenized API URL redacted."""
        text = str(error)
        if self.base_url:
            text = text.replace(self.base_url, "https://api.telegram.org/bot<redacted>")
        if self.bot_token:
            text = text.replace(self.bot_token, "<redacted>")
        return text

    def _post(
        self, method: str, params: Dict[str, Any] = None, files: Dict[str, Any] = None, timeout: int = 60
    ) -> Dict[str, Any]:
        """Make a POST request to Telegram API"""
        url = f"{self.base_url}/{method}"

        try:
            r = requests.post(url, data=params or {}, files=files, timeout=timeout)
            data = r.json()

            if not data.get("ok"):
                raise TelegramAPIError(method, data)

            return data
        except requests.exceptions.Timeout:
            raise RuntimeError(f"Telegram API timeout on {method}")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Telegram API request failed on {method}: {self._safe_exception_text(e)}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram API returned invalid JSON on {method}")

    def _get(self, method: str, params: Dict[str, Any] = None, timeout: int = 60) -> Dict[str, Any]:
        """Make a GET request to Telegram API"""
        url = f"{self.base_url}/{method}"

        try:
            r = requests.get(url, params=params or {}, timeout=timeout)
            data = r.json()

            if not data.get("ok"):
                raise TelegramAPIError(method, data)

            return data
        except requests.exceptions.Timeout:
            raise RuntimeError(f"Telegram API timeout on {method}")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Telegram API request failed on {method}: {self._safe_exception_text(e)}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram API returned invalid JSON on {method}")

    def get_me(self, timeout: int = 45) -> Dict[str, Any]:
        """Get bot information"""
        return self._get("getMe", timeout=timeout)

    def delete_webhook(self) -> None:
        """Delete webhook to enable long polling"""
        try:
            self._post("deleteWebhook", params={"drop_pending_updates": False}, timeout=45)
            log("Webhook deleted (long polling enabled)", "DEBUG")
        except Exception as e:
            log(f"Warning: Could not delete webhook: {e}", "WARN")

    def _is_get_updates_conflict(self, error: Exception) -> bool:
        """Return True when Telegram rejected long polling because another poll request is active."""
        message = str(error).lower()
        return "getupdates" in message and "409" in message and "terminated by other getupdates request" in message

    def _recover_get_updates_conflict(self, offset: int) -> Optional[List[Dict[str, Any]]]:
        """Try to take over long polling when Telegram still sees an old poll request."""
        now = time.monotonic()
        if now - self._last_update_conflict_recovery < 15:
            return None

        self._last_update_conflict_recovery = now
        log("Telegram long-poll conflict detected; attempting takeover", "WARN")

        try:
            self.delete_webhook()
            data = self._get(
                "getUpdates",
                params={"timeout": 0, "offset": offset},
                timeout=15,
            )
            log("Telegram long-poll takeover succeeded", "INFO")
            return data.get("result", [])
        except Exception as recovery_error:
            log(f"Telegram long-poll takeover failed: {recovery_error}", "WARN")
            return None

    def set_my_commands(
        self,
        commands: List[Dict[str, str]],
        scope: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Set bot commands shown in the Telegram UI menu.

        Args:
            commands: List of {"command": "start", "description": "..."} items

        Returns:
            True if successful, False otherwise
        """
        if not commands:
            return False
        try:
            params = {
                "commands": json.dumps(commands, ensure_ascii=False),
            }
            if scope:
                params["scope"] = json.dumps(scope, ensure_ascii=False)
            self._post("setMyCommands", params=params, timeout=45)
            log("Bot command menu updated", "DEBUG")
            return True
        except Exception as e:
            log(f"Could not set bot commands: {e}", "WARN")
            return False

    def delete_my_commands(
        self,
        scope: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Delete bot commands for the given Telegram command scope."""
        try:
            params: Dict[str, Any] = {}
            if scope:
                params["scope"] = json.dumps(scope, ensure_ascii=False)
            self._post("deleteMyCommands", params=params, timeout=45)
            log("Bot command menu cleared", "DEBUG")
            return True
        except Exception as e:
            log(f"Could not clear bot commands: {e}", "WARN")
            return False

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Dict] = None,
        disable_notification: bool = False,
        parse_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send text message"""
        params = {
            "chat_id": str(chat_id),
            "text": text[:4096],  # Telegram limit
            "disable_notification": disable_notification,
        }

        if parse_mode:
            params["parse_mode"] = parse_mode

        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)

        return self._post("sendMessage", params=params, timeout=60)

    def send_photo(
        self,
        chat_id: Any,
        photo_bytes: bytes,
        caption: str = "",
        reply_markup: Optional[Dict] = None,
        disable_notification: bool = False,
        has_spoiler: bool = False,
    ) -> Dict[str, Any]:
        """Send photo"""
        params = {"chat_id": str(chat_id), "caption": caption[:1024], "disable_notification": disable_notification}

        if has_spoiler:
            params["has_spoiler"] = True

        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)

        files = {"photo": ("photo.jpg", BytesIO(photo_bytes))}

        return self._post("sendPhoto", params=params, files=files, timeout=90)

    def send_video(
        self,
        chat_id: Any,
        video_bytes: bytes,
        caption: str = "",
        reply_markup: Optional[Dict] = None,
        disable_notification: bool = False,
        supports_streaming: bool = True,
        has_spoiler: bool = False,
    ) -> Dict[str, Any]:
        """Send video"""
        params = {
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            "supports_streaming": supports_streaming,
            "disable_notification": disable_notification,
        }

        if has_spoiler:
            params["has_spoiler"] = True

        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)

        files = {"video": ("video.mp4", BytesIO(video_bytes))}

        return self._post("sendVideo", params=params, files=files, timeout=180)

    def send_media_group(
        self,
        chat_id: Any,
        media_items: List[Dict[str, Any]],
        caption: str = "",
        disable_notification: bool = False,
        has_spoiler: bool = False,
    ) -> Dict[str, Any]:
        """Send a Telegram media group made from downloaded photo bytes."""
        if not isinstance(media_items, list) or len(media_items) < 2:
            raise ValueError("Telegram media groups require at least two media items")

        media_payload = []
        files: Dict[str, Any] = {}
        for index, item in enumerate(media_items[:10]):
            data = item.get("bytes") if isinstance(item, dict) else None
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise ValueError(f"Media group item {index + 1} has no bytes")

            field_name = f"photo{index}"
            payload_item: Dict[str, Any] = {
                "type": "photo",
                "media": f"attach://{field_name}",
            }
            if index == 0 and caption:
                payload_item["caption"] = caption[:1024]
            if has_spoiler:
                payload_item["has_spoiler"] = True

            media_payload.append(payload_item)
            files[field_name] = (f"gallery_{index + 1}.jpg", BytesIO(data))

        params = {
            "chat_id": str(chat_id),
            "media": json.dumps(media_payload, ensure_ascii=False),
            "disable_notification": disable_notification,
        }

        return self._post("sendMediaGroup", params=params, files=files, timeout=180)

    def send_document(
        self,
        chat_id: int,
        document_bytes: bytes,
        filename: str = "file.bin",
        caption: str = "",
        reply_markup: Optional[Dict] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Send document"""
        params = {"chat_id": str(chat_id), "caption": caption[:1024], "disable_notification": disable_notification}

        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)

        files = {"document": (filename, BytesIO(document_bytes))}

        return self._post("sendDocument", params=params, files=files, timeout=120)

    def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: Optional[Dict] = None) -> None:
        """Edit message reply markup (inline keyboard)"""
        params = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
        }

        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        else:
            params["reply_markup"] = json.dumps({"inline_keyboard": []})

        try:
            self._post("editMessageReplyMarkup", params=params, timeout=30)
        except Exception as e:
            log(f"Could not edit message markup: {e}", "WARN")

    def edit_message_text(
        self,
        chat_id: Any,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict] = None,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = True,
    ) -> bool:
        """Edit a text message and optional inline keyboard."""
        params = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "text": text[:4096],
            "disable_web_page_preview": disable_web_page_preview,
        }

        if parse_mode:
            params["parse_mode"] = parse_mode

        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)

        try:
            self._post("editMessageText", params=params, timeout=30)
            return True
        except Exception as e:
            if "message is not modified" in str(e).lower():
                return True
            log(f"Could not edit message text: {e}", "WARN")
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """Answer callback query"""
        try:
            params = {"callback_query_id": callback_query_id}
            if text:
                params["text"] = text[:200]

            self._post("answerCallbackQuery", params=params, timeout=20)
        except Exception as e:
            log(f"Could not answer callback: {e}", "WARN")

    def get_updates(self, offset: int = 0, timeout: int = 20) -> List[Dict[str, Any]]:
        """Get updates using long polling"""
        params = {"timeout": timeout, "offset": offset}

        try:
            request_timeout = max(timeout + 5, 10)
            data = self._get("getUpdates", params=params, timeout=request_timeout)
            self._get_updates_failure_count = 0
            self.get_updates_backoff_seconds = 0.0
            return data.get("result", [])
        except Exception as e:
            if self._is_get_updates_conflict(e):
                recovered = self._recover_get_updates_conflict(offset)
                if recovered is not None:
                    self._get_updates_failure_count = 0
                    self.get_updates_backoff_seconds = 0.0
                    return recovered

            self._get_updates_failure_count += 1
            retry_after = getattr(e, "retry_after", None)
            if retry_after is None:
                retry_after = min(30, 2 ** min(self._get_updates_failure_count - 1, 5))
            self.get_updates_backoff_seconds = float(max(1, retry_after))
            log(
                f"Error getting updates: {self._safe_exception_text(e)}; "
                f"retrying in {self.get_updates_backoff_seconds:.0f}s",
                "WARN",
            )
            return []

    def get_chat_member(self, chat_id: str, user_id: int) -> Dict[str, Any]:
        """Get chat member information"""
        params = {"chat_id": chat_id, "user_id": user_id}

        return self._get("getChatMember", params=params, timeout=20)

    def get_chat(self, chat_id: Any) -> Dict[str, Any]:
        """Get chat information"""
        params = {
            "chat_id": str(chat_id),
        }

        return self._get("getChat", params=params, timeout=20)

    def set_message_reaction(
        self,
        chat_id: Any,
        message_id: int,
        reaction: List[Dict[str, Any]],
        is_big: bool = False,
    ) -> bool:
        """Set one or more reactions on a message"""
        params = {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "reaction": json.dumps(reaction, ensure_ascii=False),
            "is_big": is_big,
        }

        data = self._post("setMessageReaction", params=params, timeout=30)
        return bool(data.get("result", True))

    def test_bot_token(self) -> tuple[bool, str]:
        """Test if bot token is valid"""
        last_error = "Unknown error"

        for timeout in (20, 45):
            try:
                result = self.get_me(timeout=timeout)
                username = result.get("result", {}).get("username", "unknown")
                return True, username
            except Exception as e:
                last_error = str(e)

        return False, last_error

    def test_can_message_admin(self, admin_id: int) -> tuple[bool, str]:
        """Test if bot can send messages to admin"""
        try:
            self.send_message(admin_id, "✅ Bot setup successful! You will receive approval requests here.")
            return True, "Success"
        except Exception as e:
            return False, str(e)

    def test_is_channel_admin(self, channel_username: str, bot_id: int) -> tuple[bool, str]:
        """Test if bot is admin in channel"""
        try:
            member = self.get_chat_member(channel_username, bot_id)
            status = member.get("result", {}).get("status", "")

            if status in ["administrator", "creator"]:
                return True, f"Bot is {status}"
            else:
                return False, f"Bot status is '{status}', needs to be administrator"
        except Exception as e:
            return False, str(e)

    def create_inline_keyboard(self, buttons: List[List[Dict[str, str]]]) -> Dict:
        """Create inline keyboard markup"""
        return {"inline_keyboard": buttons}

    def create_approval_keyboard(self) -> Dict:
        """Create standard approval keyboard"""
        return self.create_inline_keyboard(
            [[{"text": "✅ Approve", "callback_data": "approve"}, {"text": "❌ Skip", "callback_data": "skip"}]]
        )

    def looks_like_image(self, data: bytes) -> bool:
        """Check if bytes look like an image"""
        if not data or len(data) < 16:
            return False

        # Check for image headers
        if data.startswith(b"\xff\xd8\xff"):  # JPEG
            return True
        if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
            return True
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":  # WEBP
            return True
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):  # GIF
            return True

        return False
