"""Constants for the standalone ANIO API client."""

from typing import Final

API_URL: Final = "https://api.anio.cloud"
CLIENT_ID: Final = "anio"

TOKEN_REFRESH_BUFFER: Final = 300  # seconds before expiry to refresh

RATE_LIMIT_BACKOFF_BASE: Final = 2
RATE_LIMIT_MAX_RETRIES: Final = 5

MESSAGE_TYPE_TEXT: Final = "TEXT"
MESSAGE_TYPE_EMOJI: Final = "EMOJI"
MESSAGE_TYPE_VOICE: Final = "VOICE"

SENDER_APP: Final = "APP"
SENDER_WATCH: Final = "WATCH"
SENDER_DEVICE: Final = "DEVICE"

VALID_EMOJI_CODES: Final = [f"E{i:02d}" for i in range(1, 13)]

MAX_CHAT_MESSAGE_LENGTH: Final = 95
