"""Telegram Bot API client (stdlib only). The bot token is never logged."""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)
MAX_LEN = 4096


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str, *, dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.sent: list[dict] = []  # record of messages (used by tests and dry runs)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, params: dict[str, Any], attempts: int = 4) -> Any:
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urlencode({k: v for k, v in params.items() if v is not None}).encode()
        last = "unknown error"
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(Request(url, data=data), timeout=20) as resp:
                    payload = json.loads(resp.read().decode())
                if payload.get("ok"):
                    return payload.get("result")
                last = payload.get("description", "not ok")
            except HTTPError as e:
                try:
                    payload = json.loads(e.read().decode())
                except Exception:
                    payload = {}
                last = f"HTTP {e.code}: {payload.get('description', e.reason)}"
                if e.code == 429:
                    wait = int((payload.get("parameters") or {}).get("retry_after", 5))
                    time.sleep(min(wait, 60))
                    continue
                if e.code == 401:
                    raise TelegramError("Telegram rejected the bot token (401). Check TELEGRAM_BOT_TOKEN.")
                if e.code in (400, 403):
                    hint = ""
                    if "chat not found" in last.lower() or e.code == 403:
                        hint = (" Check TELEGRAM_CHAT_ID, and make sure you have pressed Start "
                                "in a chat with your bot.")
                    raise TelegramError(last + hint)
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last = str(e).replace(self.token, "***")
            time.sleep(2 * attempt)
        raise TelegramError(f"Telegram {method} failed after {attempts} attempts: {last}")

    def send(self, text: str, *, silent: bool = False) -> bool:
        """Send an HTML message. Returns True on success, False on failure."""
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 20].rsplit("\n", 1)[0] + "\n…(truncated)"
        record = {"text": text, "silent": silent}
        if self.dry_run:
            self.sent.append(record)
            log.debug("[dry-run] Telegram message:\n%s", text)
            return True
        if not self.configured:
            log.error("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
            return False
        try:
            self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            })
        except TelegramError as e:
            log.error("Telegram send failed: %s", e)
            return False
        self.sent.append(record)
        time.sleep(1.1)  # stay under Telegram's per-chat rate limit
        return True

    def get_updates(self) -> list[dict]:
        return self._call("getUpdates", {"timeout": 0}) or []

    def get_me(self) -> dict:
        return self._call("getMe", {})
