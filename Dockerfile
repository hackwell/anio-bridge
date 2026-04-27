FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ffmpeg is only needed if WHISPER_ENABLED=true; small enough to keep in.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY anio ./anio
COPY telegram ./telegram
COPY bridge ./bridge
COPY main.py whisper_transcribe.py ./

# Optional: install whisper Python package by passing
#   --build-arg INSTALL_WHISPER=1
ARG INSTALL_WHISPER=0
RUN if [ "$INSTALL_WHISPER" = "1" ]; then \
        pip install --no-cache-dir openai-whisper; \
    fi

RUN mkdir -p /data
VOLUME ["/data"]

ENV STATE_FILE=/data/state.json \
    POLL_INTERVAL=60 \
    LOG_LEVEL=INFO

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "main.py"]
