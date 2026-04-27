"""Parse Telegram messages from Jörg into ANIO send requests."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Accepted prefixes (case-insensitive) for outgoing messages to the watch:
#   - "Schreib Marla:" / "Schreibe Marla:" / "Schreib Marla "
#   - "/send <text>" / "/marla <text>" / "/msg <text>"
_PATTERN = re.compile(
    r"""^\s*
        (?:
            schreib(?:e)?\s+marla\s*[:,\-]?\s*
            | /send(?:@\w+)?\s+
            | /marla(?:@\w+)?\s+
            | /msg(?:@\w+)?\s+
        )
        (?P<text>.+?)\s*$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class MessageRequest:
    """Parsed request to forward to the watch."""

    text: str


def parse_telegram_text(raw: str | None) -> MessageRequest | None:
    """Return a MessageRequest if `raw` matches a known prefix, else None."""
    if not raw:
        return None
    m = _PATTERN.match(raw)
    if not m:
        return None
    text = m.group("text").strip()
    if not text:
        return None
    return MessageRequest(text=text)
