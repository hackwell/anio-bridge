"""Whisper transcription for ANIO voice messages.

Lazy + best-effort. Two backends are tried, in order:
  1. The `openai-whisper` Python package (if installed).
  2. The `whisper` CLI on $PATH (via asyncio subprocess, no shell).

If neither is available, transcription gracefully degrades to a no-op
and callers get back ``None``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "tiny")
DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "de")


class VoiceTranscriber:
    """Wraps a Whisper backend with a uniform async transcribe interface."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self._model_name = model_name
        self._language = language
        self._py_model: Any | None = None
        self._backend: str | None = None
        self._init_attempted = False

    @property
    def available(self) -> bool:
        if not self._init_attempted:
            self._initialise()
        return self._backend is not None

    def _initialise(self) -> None:
        self._init_attempted = True
        try:
            import whisper  # type: ignore[import-not-found]
        except ImportError:
            whisper = None  # type: ignore[assignment]

        if whisper is not None:
            try:
                self._py_model = whisper.load_model(self._model_name)
                self._backend = "python"
                _LOGGER.info(
                    "Whisper Python backend ready (model=%s)", self._model_name
                )
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Could not load whisper model: %s", err)

        if shutil.which("whisper"):
            self._backend = "cli"
            _LOGGER.info("Whisper CLI backend ready (model=%s)", self._model_name)
            return

        _LOGGER.info("Whisper unavailable — voice messages will not be transcribed")

    async def transcribe(self, audio_bytes: bytes) -> str | None:
        if not self.available:
            return None

        suffix = ".ogg"  # ANIO voice notes are typically ogg/opus
        with tempfile.NamedTemporaryFile(
            prefix="anio-voice-", suffix=suffix, delete=False
        ) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            if self._backend == "python":
                return await asyncio.to_thread(self._transcribe_python, tmp_path)
            if self._backend == "cli":
                return await self._transcribe_cli(tmp_path)
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _transcribe_python(self, path: str) -> str | None:
        if self._py_model is None:
            return None
        result = self._py_model.transcribe(
            path,
            language=self._language,
            fp16=False,
        )
        text = (result.get("text") or "").strip()
        return text or None

    async def _transcribe_cli(self, path: str) -> str | None:
        out_dir = tempfile.mkdtemp(prefix="whisper-")
        try:
            proc = await asyncio.create_subprocess_exec(
                "whisper",
                path,
                "--model",
                self._model_name,
                "--language",
                self._language,
                "--output_format",
                "txt",
                "--output_dir",
                out_dir,
                "--fp16",
                "False",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                _LOGGER.warning(
                    "whisper CLI failed (%d): %s",
                    proc.returncode,
                    stderr.decode(errors="replace")[:500],
                )
                return None

            base = os.path.splitext(os.path.basename(path))[0]
            txt_path = os.path.join(out_dir, base + ".txt")
            if not os.path.exists(txt_path):
                return None
            with open(txt_path, encoding="utf-8") as f:
                text = f.read().strip()
            return text or None
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
