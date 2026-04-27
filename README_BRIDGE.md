# Anio Bridge

Standalone Python service that bridges the **Anio Watch** of Marla Weller with
**Telegram** for Jörg.

> The Home Assistant integration in `custom_components/anio/` is unrelated and
> still ships from this repo. The bridge re-uses the API client patterns under
> a separate `anio/` package at the repo root.

## Features

1. **Telegram → Watch.** Jörg sends the bot messages like
   `Schreib Marla: Essen ist fertig!` and the text is forwarded to the watch
   (max. 95 chars, truncated otherwise). `/send`, `/marla`, and `/msg` work too.
2. **Watch → Telegram.** Every `POLL_INTERVAL` seconds the bridge polls the
   Anio activity feed. New `MESSAGE` items from sender `WATCH`/`DEVICE` are
   forwarded as `📱 Marla: …`. Seen IDs are persisted in the state file so a
   restart never re-sends the backlog.
3. **Voice notes.** When a voice message arrives, the bridge attempts to
   transcribe it via Whisper (Python package or `whisper` CLI). If Whisper is
   not available it falls back to `[Sprachnachricht] (Transkription nicht
   verfügbar)`.

## Layout

```
main.py                  # Entry point + signal handling
anio/                    # Standalone Anio API client
telegram/                # Long-polling Telegram client + parser
bridge/                  # State persistence + poller
whisper_transcribe.py    # Optional Whisper backend
Dockerfile, requirements.txt
```

## Configuration (env vars)

| Name                  | Required | Default          | Notes                                       |
| --------------------- | -------- | ---------------- | ------------------------------------------- |
| `ANIO_EMAIL`          | yes      | —                | Anio account email                          |
| `ANIO_PASSWORD`       | yes      | —                | Anio account password                       |
| `ANIO_DEVICE_ID`      | no       | first device     | Pin to a specific watch                     |
| `ANIO_SENDER_NAME`    | no       | `Papa`           | Sender name shown on the watch              |
| `TELEGRAM_BOT_TOKEN`  | yes      | —                | BotFather token                             |
| `TELEGRAM_CHAT_ID`    | yes      | —                | Jörg's numeric chat id (e.g. `1433010035`)  |
| `POLL_INTERVAL`       | no       | `60`             | Activity-feed poll period in seconds        |
| `STATE_FILE`          | no       | `/data/state.json` | Persisted state (seen IDs, tokens, offset) |
| `WHISPER_ENABLED`     | no       | `false`          | Enable voice transcription                  |
| `WHISPER_MODEL`       | no       | `tiny`           | Whisper model name                          |
| `WHISPER_LANGUAGE`    | no       | `de`             | Forced language                             |
| `LOG_LEVEL`           | no       | `INFO`           | Standard logging levels                     |

## Local run

```bash
pip install -r requirements.txt
export ANIO_EMAIL=... ANIO_PASSWORD=... \
       TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=1433010035 \
       STATE_FILE=./state.json
python main.py
```

## Docker / Coolify

```bash
docker build -t anio-bridge .
# Optional: bake whisper into the image (much larger)
# docker build --build-arg INSTALL_WHISPER=1 -t anio-bridge .

docker run -d --restart=unless-stopped \
  -e ANIO_EMAIL=... -e ANIO_PASSWORD=... \
  -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_CHAT_ID=1433010035 \
  -v anio-bridge-data:/data \
  --name anio-bridge anio-bridge
```

In Coolify: deploy as a Docker application, mount a persistent volume to
`/data`, and set the env vars above.

## Notes

- **Auth:** The bridge logs in once and then refreshes via
  `POST /v1/auth/refresh-access-token`. Tokens are persisted in the state file
  so restarts don't need a fresh login.
- **Backlog protection:** On the first poll after a (re)start the bridge primes
  the seen-set with the current activity feed instead of forwarding it. New
  messages arriving after that are forwarded normally.
- **Authorization:** Only messages from `TELEGRAM_CHAT_ID` are processed.
  Anything else is logged and dropped.
- **Graceful shutdown:** SIGTERM/SIGINT trigger a clean shutdown; the state is
  flushed and the bot sends a final `🔴 Anio-Bridge stoppt.`.
