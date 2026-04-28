"""Anio → Telegram polling loop. Forwards new watch messages to Jörg."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from anio import (
    ActivityItem,
    AnioApiClient,
    AnioApiError,
    AnioConnectionError,
)
from anio.const import SENDER_DEVICE, SENDER_WATCH

if TYPE_CHECKING:
    from bridge.hermes_forwarder import HermesForwarder
    from telegram.bot import TelegramBot
    from whisper_transcribe import VoiceTranscriber

    from .state import BridgeState

_LOGGER = logging.getLogger(__name__)

VOICE_URL_KEYS = ("voiceUrl", "audioUrl", "url", "fileUrl", "mediaUrl")


class AnioPoller:
    """Polls the ANIO activity feed and forwards new WATCH messages to Telegram."""

    def __init__(
        self,
        client: AnioApiClient,
        bot: TelegramBot,
        state: BridgeState,
        owner_chat_id: int,
        device_name: str,
        poll_interval: float,
        transcriber: VoiceTranscriber | None = None,
    ) -> None:
        self._client = client
        self._bot = bot
        self._state = state
        self._owner_chat_id = owner_chat_id
        self._device_name = device_name
        self._poll_interval = poll_interval
        self._transcriber = transcriber
        self._stop = asyncio.Event()
        self._priming = True

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        _LOGGER.info(
            "Anio poller started (every %.0fs, device=%s)",
            self._poll_interval,
            self._device_name,
        )
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except (AnioConnectionError, AnioApiError) as err:
                _LOGGER.warning("Anio poll failed: %s", err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected poll error: %s", err)
            self._priming = False
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass
        _LOGGER.info("Anio poller stopped")

    async def _poll_once(self) -> None:
        activity = await self._client.get_activity()
        new_count = 0
        save_needed = False

        for item in activity:
            if not self._is_watch_message(item):
                continue
            assert item.data is not None
            message_id = str(item.data.get("id") or item.id)

            if message_id in self._state.seen_set:
                continue

            self._state.mark_seen(message_id)
            save_needed = True

            if self._priming:
                # Don't blast the user with backlog on first run after restart.
                _LOGGER.debug("Priming seen-set with %s", message_id)
                continue

            new_count += 1
            await self._forward(item.data)

        if save_needed:
            await self._state.save()

        if new_count:
            _LOGGER.info("Forwarded %d new watch message(s)", new_count)

    @staticmethod
    def _is_watch_message(item: ActivityItem) -> bool:
        if item.type != "MESSAGE" or not item.data:
            return False
        sender = item.data.get("sender")
        return sender in (SENDER_WATCH, SENDER_DEVICE)

    async def _forward(self, data: dict[str, Any]) -> None:
        msg_type = (data.get("type") or "TEXT").upper()
        text = (data.get("text") or "").strip()

        if msg_type == "VOICE":
            await self._forward_voice(data)
            return

        if msg_type == "EMOJI":
            body = f"📱 {self._device_name}: {text or '(Emoji)'}"
        elif text:
            body = f"📱 {self._device_name}: {text}"
        else:
            body = f"📱 {self._device_name}: (leere Nachricht)"

        await self._bot.send_message(self._owner_chat_id, body)

    async def _forward_voice(self, data: dict[str, Any]) -> None:
        message_id = str(data.get("id") or "")
        prefix = f"📱 {self._device_name} [Sprachnachricht]"

        # Wenn der Bot ein HermesForwarder ist, Audio-URL direkt übergeben
        # damit Hermes selbst transkribiert
        if hasattr(self._bot, "send_voice_event"):
            audio_url = ""
            for key in VOICE_URL_KEYS:
                url = data.get(key)
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    audio_url = url
                    break
            await self._bot.send_voice_event(  # type: ignore[union-attr]
                self._device_name, audio_url, message_id
            )
            return

        transcription = await self._try_transcribe(data, message_id)
        if transcription:
            body = f"{prefix}: {transcription}"
        else:
            body = f"{prefix}: (Transkription nicht verfügbar)"
        await self._bot.send_message(self._owner_chat_id, body)

    async def _try_transcribe(
        self, data: dict[str, Any], message_id: str
    ) -> str | None:
        if not self._transcriber or not self._transcriber.available:
            return None

        audio = await self._fetch_audio(data, message_id)
        if not audio:
            return None
        try:
            return await self._transcriber.transcribe(audio)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Transcription failed: %s", err)
            return None

    async def _fetch_audio(
        self, data: dict[str, Any], message_id: str
    ) -> bytes | None:
        for key in VOICE_URL_KEYS:
            url = data.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                audio = await self._download(url)
                if audio:
                    return audio
        if message_id:
            return await self._client.download_voice(message_id)
        return None

    async def _download(self, url: str) -> bytes | None:
        # Reuse the api client's session for connection pooling.
        session = self._client._session  # noqa: SLF001
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.debug("Voice download %s -> %d", url, resp.status)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Voice download error %s: %s", url, err)
        return None
