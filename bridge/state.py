"""Persistent state for the bridge (seen message IDs, tokens, offset)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

MAX_SEEN_IDS = 1000


@dataclass
class BridgeState:
    """In-memory state, persisted to disk as JSON."""

    path: Path
    seen_message_ids: list[str] = field(default_factory=list)
    telegram_offset: int | None = None
    anio_access_token: str | None = None
    anio_refresh_token: str | None = None
    app_uuid: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> BridgeState:
        p = Path(path)
        if not p.exists():
            _LOGGER.info("State file %s missing — starting empty", p)
            p.parent.mkdir(parents=True, exist_ok=True)
            return cls(path=p)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.error("Cannot read state %s: %s — starting empty", p, err)
            return cls(path=p)
        return cls(
            path=p,
            seen_message_ids=list(data.get("seen_message_ids", []))[-MAX_SEEN_IDS:],
            telegram_offset=data.get("telegram_offset"),
            anio_access_token=data.get("anio_access_token"),
            anio_refresh_token=data.get("anio_refresh_token"),
            app_uuid=data.get("app_uuid"),
        )

    @property
    def seen_set(self) -> set[str]:
        return set(self.seen_message_ids)

    def mark_seen(self, message_id: str) -> bool:
        """Add ID to seen list. Returns True if newly added."""
        if message_id in self.seen_message_ids:
            return False
        self.seen_message_ids.append(message_id)
        if len(self.seen_message_ids) > MAX_SEEN_IDS:
            self.seen_message_ids = self.seen_message_ids[-MAX_SEEN_IDS:]
        return True

    async def save(self) -> None:
        """Atomically persist state to disk."""
        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    def _save_sync(self) -> None:
        payload = {
            "seen_message_ids": self.seen_message_ids,
            "telegram_offset": self.telegram_offset,
            "anio_access_token": self.anio_access_token,
            "anio_refresh_token": self.anio_refresh_token,
            "app_uuid": self.app_uuid,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
