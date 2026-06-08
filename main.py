"""Anio ↔ Telegram bridge — entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

import aiohttp

from anio import (
    AnioApiClient,
    AnioApiError,
    AnioAuth,
    AnioAuthError,
    AnioConnectionError,
    AnioMessageTooLongError,
)
from anio.const import MAX_CHAT_MESSAGE_LENGTH
from bridge.hermes_forwarder import HermesForwarder
from bridge.poller import AnioPoller
from bridge.state import BridgeState
from telegram.bot import TelegramBot
from telegram.handlers import parse_telegram_text
from whisper_transcribe import VoiceTranscriber

_LOGGER = logging.getLogger("anio_bridge")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    # aiohttp access logs are noisy; we don't run a server, but tame third-parties.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 1:
        return text[:limit], True
    return text[: limit - 1].rstrip() + "…", True


class Bridge:
    def __init__(self) -> None:
        self.email = _require_env("ANIO_EMAIL")
        self.password = _require_env("ANIO_PASSWORD")
        self.tg_token = _require_env("TELEGRAM_BOT_TOKEN")
        self.tg_chat_id = int(_require_env("TELEGRAM_CHAT_ID"))
        self.poll_interval = float(os.getenv("POLL_INTERVAL", "60"))
        self.state_file = os.getenv("STATE_FILE", "/data/state.json")
        self.sender_name = os.getenv("ANIO_SENDER_NAME", "Papa")
        self.device_id_override = os.getenv("ANIO_DEVICE_ID") or None
        self.whisper_enabled = os.getenv("WHISPER_ENABLED", "false").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        # Hermes-Webhook-Modus: wenn gesetzt, wird kein Telegram-Bot verwendet
        self.hermes_webhook_url = os.getenv("HERMES_WEBHOOK_URL") or None
        self.hermes_webhook_secret = os.getenv("HERMES_WEBHOOK_SECRET", "")

        self.state = BridgeState.load(self.state_file)

        # Fallback: Wenn state.json keine Tokens hat, aus Env-Variablen laden.
        # Dies greift wenn das Docker-Volume leer ist (z.B. nach Neuanlage des Containers).
        # Die Env-Variablen ANIO_REFRESH_TOKEN / ANIO_APP_UUID werden von Hermes
        # nach erfolgreichem Login automatisch aktualisiert.
        if not self.state.anio_refresh_token:
            env_refresh = os.getenv("ANIO_REFRESH_TOKEN")
            env_app_uuid = os.getenv("ANIO_APP_UUID")
            if env_refresh:
                _LOGGER.info(
                    "Kein Token in state.json — lade Refresh-Token aus Env-Variable ANIO_REFRESH_TOKEN"
                )
                self.state.anio_refresh_token = env_refresh
                if env_app_uuid:
                    self.state.app_uuid = env_app_uuid

        self.session: aiohttp.ClientSession | None = None
        self.auth: AnioAuth | None = None
        self.client: AnioApiClient | None = None
        self.bot: TelegramBot | None = None
        self.poller: AnioPoller | None = None
        self.device_id: str | None = None
        self.device_name: str = "Marla"
        self.transcriber: VoiceTranscriber | None = None
        self._stopping = asyncio.Event()

    async def _on_token_refresh(self, access: str, refresh: str) -> None:
        self.state.anio_access_token = access
        self.state.anio_refresh_token = refresh
        if self.auth:
            self.state.app_uuid = self.auth.app_uuid
        await self.state.save()
        # Refresh-Token auch in Coolify Env-Variable aktualisieren (Fallback für nächsten Neustart)
        await self._update_coolify_refresh_token(refresh)

    async def _update_coolify_refresh_token(self, refresh_token: str) -> None:
        """Aktualisiert ANIO_REFRESH_TOKEN in Coolify Env-Variablen (bester-Effort)."""
        coolify_api_url = os.getenv("COOLIFY_API_URL")
        coolify_token = os.getenv("COOLIFY_API_TOKEN")
        coolify_app_uuid = os.getenv("COOLIFY_APP_UUID")
        if not (coolify_api_url and coolify_token and coolify_app_uuid):
            return  # Coolify-Integration nicht konfiguriert
        try:
            assert self.session is not None
            headers = {
                "Authorization": f"Bearer {coolify_token}",
                "Content-Type": "application/json",
            }
            # Bestehende Env-Variable finden
            async with self.session.get(
                f"{coolify_api_url}/api/v1/applications/{coolify_app_uuid}/envs",
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    return
                envs = await resp.json()

            env_uuid = next(
                (e["uuid"] for e in envs if e["key"] == "ANIO_REFRESH_TOKEN"),
                None,
            )
            if env_uuid:
                async with self.session.patch(
                    f"{coolify_api_url}/api/v1/applications/{coolify_app_uuid}/envs/{env_uuid}",
                    headers=headers,
                    json={"key": "ANIO_REFRESH_TOKEN", "value": refresh_token},
                ) as resp:
                    if resp.status == 200:
                        _LOGGER.debug("ANIO_REFRESH_TOKEN in Coolify aktualisiert")
                    else:
                        _LOGGER.warning("Coolify Env-Update fehlgeschlagen: %d", resp.status)
            else:
                async with self.session.post(
                    f"{coolify_api_url}/api/v1/applications/{coolify_app_uuid}/envs",
                    headers=headers,
                    json={"key": "ANIO_REFRESH_TOKEN", "value": refresh_token, "is_preview": False},
                ) as resp:
                    if resp.status == 201:
                        _LOGGER.debug("ANIO_REFRESH_TOKEN in Coolify erstellt")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Coolify Env-Update fehlgeschlagen (ignoriert): %s", err)

    async def setup(self) -> None:
        timeout = aiohttp.ClientTimeout(total=60)
        self.session = aiohttp.ClientSession(timeout=timeout)

        self.auth = AnioAuth(
            session=self.session,
            email=self.email,
            password=self.password,
            access_token=self.state.anio_access_token,
            refresh_token=self.state.anio_refresh_token,
            app_uuid=self.state.app_uuid,
            on_token_refresh=self._on_token_refresh,
        )
        self.state.app_uuid = self.auth.app_uuid

        self.client = AnioApiClient(self.session, self.auth)

        # Force token acquisition once at startup so we fail fast on bad creds.
        await self.auth.ensure_valid_token()

        await self._resolve_device()

        if self.whisper_enabled:
            self.transcriber = VoiceTranscriber()
            # Trigger lazy init so we log backend availability now.
            _ = self.transcriber.available

        if self.hermes_webhook_url:
            _LOGGER.info("Hermes-Webhook-Modus aktiv: %s", self.hermes_webhook_url)
            self.bot = HermesForwarder(  # type: ignore[assignment]
                session=self.session,
                webhook_url=self.hermes_webhook_url,
                webhook_secret=self.hermes_webhook_secret,
                owner_chat_id=self.tg_chat_id,
            )
        else:
            self.bot = TelegramBot(
                session=self.session,
                token=self.tg_token,
                allowed_chat_id=self.tg_chat_id,
                on_update=self._handle_telegram_message,
                initial_offset=self.state.telegram_offset,
            )

        self.poller = AnioPoller(
            client=self.client,
            bot=self.bot,
            state=self.state,
            owner_chat_id=self.tg_chat_id,
            device_name=self.device_name,
            poll_interval=self.poll_interval,
            transcriber=self.transcriber,
        )

    async def _resolve_device(self) -> None:
        assert self.client is not None
        devices = await self.client.get_devices()
        if not devices:
            raise SystemExit("No ANIO devices found for this account")

        if self.device_id_override:
            for d in devices:
                if d.id == self.device_id_override:
                    self.device_id = d.id
                    self.device_name = d.settings.name
                    break
            else:
                raise SystemExit(
                    f"Configured ANIO_DEVICE_ID {self.device_id_override} "
                    "not found on account"
                )
        else:
            d = devices[0]
            self.device_id = d.id
            self.device_name = d.settings.name

        _LOGGER.info(
            "Using device %s (%s)", self.device_name, self.device_id
        )

    async def _handle_telegram_message(self, message: dict[str, Any]) -> None:
        assert self.bot is not None and self.client is not None and self.device_id

        # Persist offset early so we don't reprocess on crash.
        self.state.telegram_offset = self.bot.offset
        await self.state.save()

        text = message.get("text") or message.get("caption")
        if not text:
            await self.bot.send_message(
                self.tg_chat_id,
                "Konnte Nachricht nicht lesen (kein Text).",
            )
            return

        parsed = parse_telegram_text(text)
        if not parsed:
            await self.bot.send_message(
                self.tg_chat_id,
                'Bitte mit "Schreib Marla: ..." oder /send ... beginnen.',
            )
            return

        outgoing, truncated = _truncate(parsed.text, MAX_CHAT_MESSAGE_LENGTH)

        try:
            await self.client.send_text_message(
                self.device_id,
                outgoing,
                username=self.sender_name,
            )
        except AnioMessageTooLongError as err:
            await self.bot.send_message(
                self.tg_chat_id,
                f"Nachricht zu lang ({err.length}/{err.max_length}).",
            )
            return
        except AnioAuthError as err:
            _LOGGER.error("Auth error sending message: %s", err)
            await self.bot.send_message(
                self.tg_chat_id, "Auth-Fehler bei Anio. Bitte Logs prüfen."
            )
            return
        except (AnioConnectionError, AnioApiError) as err:
            _LOGGER.warning("Send failed: %s", err)
            await self.bot.send_message(
                self.tg_chat_id, f"Senden fehlgeschlagen: {err}"
            )
            return

        if truncated:
            confirm = (
                f"✅ An {self.device_name} (gekürzt auf "
                f"{MAX_CHAT_MESSAGE_LENGTH} Zeichen): {outgoing}"
            )
        else:
            confirm = f"✅ An {self.device_name}: {outgoing}"
        await self.bot.send_message(self.tg_chat_id, confirm)

    async def run(self) -> None:
        assert self.bot is not None and self.poller is not None
        await self.bot.notify_owner(
            f"🟢 Anio-Bridge gestartet — Gerät {self.device_name}."
        )

        async with asyncio.TaskGroup() as tg:
            poll_task = tg.create_task(self.poller.run(), name="anio-poller")
            bot_task = tg.create_task(self.bot.run(), name="telegram-bot")

            await self._stopping.wait()
            self.poller.stop()
            self.bot.stop()
            # Tasks self-exit when their stop events trigger.
            del poll_task, bot_task

    async def shutdown(self) -> None:
        _LOGGER.info("Shutting down")
        self._stopping.set()
        try:
            if self.bot:
                await self.bot.notify_owner("🔴 Anio-Bridge stoppt.")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Shutdown notification failed: %s", err)
        await self.state.save()
        if self.session:
            await self.session.close()


async def _amain() -> None:
    _setup_logging()
    bridge = Bridge()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge._stopping.set)

    try:
        await bridge.setup()
        await bridge.run()
    except AnioAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        await bridge.shutdown()
        raise SystemExit(2) from None
    except SystemExit:
        await bridge.shutdown()
        raise
    else:
        await bridge.shutdown()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
