"""Minimal async Telegram Bot client (long polling + send).

Uses direct HTTP calls so we avoid the python-telegram-bot dependency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

UpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]

LONG_POLL_TIMEOUT = 30  # seconds; Telegram holds the connection up to this long
LONG_POLL_HTTP_TIMEOUT = LONG_POLL_TIMEOUT + 10
SEND_HTTP_TIMEOUT = 15


class TelegramBot:
    """Async Telegram Bot client with long polling."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        allowed_chat_id: int,
        on_update: UpdateHandler,
        initial_offset: int | None = None,
    ) -> None:
        self._session = session
        self._token = token
        self._allowed_chat_id = allowed_chat_id
        self._on_update = on_update
        self._offset = initial_offset
        self._stop = asyncio.Event()

    @property
    def offset(self) -> int | None:
        return self._offset

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self._token}"

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        """Send a message to a chat. Logs and swallows transient errors."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            async with self._session.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=SEND_HTTP_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    _LOGGER.error(
                        "Telegram sendMessage failed (%d): %s", resp.status, body
                    )
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("Telegram sendMessage error: %s", err)

    async def notify_owner(self, text: str) -> None:
        """Send a message to the configured owner/allowed chat."""
        await self.send_message(self._allowed_chat_id, text)

    async def get_file_url(self, file_id: str) -> str | None:
        """Resolve a Telegram file_id to a downloadable URL."""
        try:
            async with self._session.get(
                f"{self.base_url}/getFile",
                params={"file_id": file_id},
                timeout=aiohttp.ClientTimeout(total=SEND_HTTP_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("getFile failed: %d", resp.status)
                    return None
                data = await resp.json()
                file_path = data.get("result", {}).get("file_path")
                if not file_path:
                    return None
                return f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("getFile error: %s", err)
            return None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Main long-polling loop. Returns when stop() is called."""
        _LOGGER.info("Telegram long polling started")
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = await self._get_updates()
                backoff = 1.0
                for update in updates:
                    await self._dispatch(update)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("Telegram poll network error: %s", err)
                await self._wait_backoff(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Telegram poll error: %s", err)
                await self._wait_backoff(backoff)
                backoff = min(backoff * 2, 60.0)
        _LOGGER.info("Telegram long polling stopped")

    async def _get_updates(self) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": LONG_POLL_TIMEOUT,
            "allowed_updates": '["message"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        async with self._session.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=aiohttp.ClientTimeout(total=LONG_POLL_HTTP_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise aiohttp.ClientError(
                    f"getUpdates {resp.status}: {body[:200]}"
                )
            data = await resp.json()
        if not data.get("ok"):
            raise aiohttp.ClientError(f"Telegram error: {data.get('description')}")
        return data.get("result", [])

    async def _dispatch(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self._offset = update_id + 1

        message = update.get("message")
        if not message:
            return

        chat_id = message.get("chat", {}).get("id")
        if chat_id != self._allowed_chat_id:
            _LOGGER.warning(
                "Ignored message from unauthorized chat_id %s", chat_id
            )
            return

        try:
            await self._on_update(message)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error in update handler")

    async def _wait_backoff(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
