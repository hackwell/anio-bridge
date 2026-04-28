"""Hermes Webhook Forwarder — ersetzt TelegramBot für ausgehende Nachrichten.

Statt Nachrichten direkt per Telegram zu senden, schickt diese Klasse
einen HTTP-POST an den Hermes-Webhook. Hermes verarbeitet die Nachricht
(Transkription, Interpretation) und informiert Jörg auf Telegram.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class HermesForwarder:
    """Sendet Anio-Ereignisse via HTTP-Webhook an Hermes zur Verarbeitung."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        webhook_url: str,
        webhook_secret: str,
        owner_chat_id: int,
    ) -> None:
        self._session = session
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._owner_chat_id = owner_chat_id
        self._stop = asyncio.Event()

    @property
    def offset(self) -> int | None:
        return None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Kompatibilitätsmethode — Forwarder braucht keine eigene Run-Loop."""
        await self._stop.wait()

    async def notify_owner(self, text: str) -> None:
        """Schickt eine Statusnachricht direkt an Hermes (z.B. Start/Stop)."""
        await self._post_to_hermes({
            "type": "status",
            "text": text,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        """Schickt eine fertig aufbereitete Textnachricht an Hermes."""
        await self._post_to_hermes({
            "type": "text",
            "text": text,
            "audio_url": "",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    async def send_voice_event(
        self,
        device_name: str,
        audio_url: str,
        message_id: str,
    ) -> None:
        """Schickt ein Sprachnachrichten-Ereignis an Hermes zur Transkription."""
        await self._post_to_hermes({
            "type": "voice",
            "text": f"Sprachnachricht von {device_name}",
            "audio_url": audio_url,
            "message_id": message_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    async def _post_to_hermes(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        signature = hmac.new(
            self._webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        try:
            async with self._session.post(
                self._webhook_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hermes-Signature-256": f"sha256={signature}",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 202, 204):
                    text = await resp.text()
                    _LOGGER.warning(
                        "Hermes webhook returned %d: %s", resp.status, text[:200]
                    )
                else:
                    _LOGGER.debug("Hermes webhook OK (%d)", resp.status)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Hermes webhook post failed: %s", err)
