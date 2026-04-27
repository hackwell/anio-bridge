"""ANIO API client (standalone, ported from HA integration)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

from .const import (
    API_URL,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_MAX_RETRIES,
    VALID_EMOJI_CODES,
)
from .exceptions import (
    AnioApiError,
    AnioAuthError,
    AnioConnectionError,
    AnioDeviceNotFoundError,
    AnioMessageTooLongError,
    AnioRateLimitError,
)
from .models import (
    ActivityItem,
    ChatMessage,
    Device,
    DeviceLocation,
)

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .auth import AnioAuth

_LOGGER = logging.getLogger(__name__)


class AnioApiClient:
    """Client for the ANIO Cloud API."""

    def __init__(self, session: ClientSession, auth: AnioAuth) -> None:
        self._session = session
        self._auth = auth
        self._retry_count = 0

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: dict,
    ) -> dict | list | None:
        token = await self._auth.ensure_valid_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "app-uuid": self._auth.app_uuid,
            "Content-Type": "application/json",
            **kwargs.pop("headers", {}),
        }
        url = f"{API_URL}{endpoint}"

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                **kwargs,
            ) as response:
                if response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    await self._handle_rate_limit(retry_after)
                    return await self._request(method, endpoint, **kwargs)

                if response.status == 401:
                    raise AnioAuthError("Access token rejected by server")

                if response.status == 404:
                    raise AnioDeviceNotFoundError("unknown")

                if response.status >= 400:
                    text = await response.text()
                    raise AnioApiError(f"API error: {text}", response.status)

                self._retry_count = 0

                if response.status == 204:
                    return None

                return await response.json()

        except aiohttp.ClientError as err:
            raise AnioConnectionError(f"Connection failed: {err}") from err

    async def _handle_rate_limit(self, retry_after: str | None) -> None:
        self._retry_count += 1
        if self._retry_count > RATE_LIMIT_MAX_RETRIES:
            self._retry_count = 0
            raise AnioRateLimitError("Max retries exceeded")

        if retry_after:
            wait_time = int(retry_after)
        else:
            wait_time = RATE_LIMIT_BACKOFF_BASE**self._retry_count

        _LOGGER.warning(
            "Rate limited, waiting %ds (attempt %d/%d)",
            wait_time,
            self._retry_count,
            RATE_LIMIT_MAX_RETRIES,
        )
        await asyncio.sleep(wait_time)

    async def get_devices(self) -> list[Device]:
        data = await self._request("GET", "/v1/device/list")
        if not isinstance(data, list):
            return []
        return [Device.model_validate(d) for d in data]

    async def send_text_message(
        self,
        device_id: str,
        text: str,
        username: str | None = None,
        max_length: int = 95,
    ) -> ChatMessage:
        if len(text) > max_length:
            raise AnioMessageTooLongError(len(text), max_length)

        payload: dict[str, str] = {"deviceId": device_id, "text": text}
        if username:
            payload["username"] = username

        data = await self._request("POST", "/v1/chat/message/text", json=payload)
        return ChatMessage.model_validate(data)

    async def send_emoji_message(
        self,
        device_id: str,
        emoji_code: str,
        username: str | None = None,
    ) -> ChatMessage:
        if emoji_code not in VALID_EMOJI_CODES:
            raise AnioApiError(
                f"Invalid emoji code: {emoji_code}. Valid: {VALID_EMOJI_CODES}"
            )
        payload: dict[str, str] = {"deviceId": device_id, "text": emoji_code}
        if username:
            payload["username"] = username
        data = await self._request("POST", "/v1/chat/message/emoji", json=payload)
        return ChatMessage.model_validate(data)

    async def get_activity(self) -> list[ActivityItem]:
        data = await self._request("GET", "/v1/activity")
        if not isinstance(data, list):
            return []
        result: list[ActivityItem] = []
        for item in data:
            try:
                result.append(ActivityItem.model_validate(item))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Failed to parse activity item: %s", err)
        return result

    async def get_chat_history(self, device_id: str) -> list[ChatMessage]:
        try:
            data = await self._request("GET", f"/v1/chat/{device_id}")
            if not isinstance(data, list):
                return []
            result: list[ChatMessage] = []
            for msg in data:
                try:
                    result.append(ChatMessage.model_validate(msg))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Failed to parse chat message: %s", err)
            return result
        except AnioDeviceNotFoundError:
            _LOGGER.debug("No chat history for device %s (404)", device_id)
            return []

    async def get_last_location(self, device_id: str) -> DeviceLocation | None:
        try:
            data = await self._request("GET", f"/v1/location/{device_id}/last")
            if not isinstance(data, dict):
                return None
            return DeviceLocation.model_validate(data)
        except AnioDeviceNotFoundError:
            return None

    async def download_voice(self, message_id: str) -> bytes | None:
        """Try common voice download endpoints. Returns None if unavailable."""
        token = await self._auth.ensure_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "app-uuid": self._auth.app_uuid,
        }
        candidates = [
            f"/v1/chat/message/{message_id}/voice",
            f"/v1/chat/voice/{message_id}",
            f"/v1/chat/message/voice/{message_id}",
            f"/v1/chat/{message_id}/voice",
        ]
        for endpoint in candidates:
            try:
                async with self._session.get(
                    f"{API_URL}{endpoint}",
                    headers=headers,
                ) as response:
                    if response.status == 200:
                        _LOGGER.debug("Voice fetched from %s", endpoint)
                        return await response.read()
                    if response.status not in (404, 405):
                        _LOGGER.debug(
                            "Voice endpoint %s returned %d",
                            endpoint,
                            response.status,
                        )
            except aiohttp.ClientError as err:
                _LOGGER.debug("Voice fetch error %s: %s", endpoint, err)
        return None
