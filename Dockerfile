FROM ubuntu:24.04

ARG AUDIVERIS_VERSION=5.11.0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    AUDIVERIS_BIN=/opt/audiveris/bin/Audiveris \
    TESSDATA_PREFIX=/opt/tessdata \
    HOME=/tmp

# Audiveris bundles Tesseract but its OCR needs the full (legacy-capable) tessdata models.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-venv \
      fontconfig libfreetype6 fonts-dejavu-core libasound2t64 libxi6 libxrender1 libxtst6 libgtk-3-0t64 \
 && curl -fsSL -o /tmp/audiveris.deb \
      "https://github.com/Audiveris/audiveris/releases/download/${AUDIVERIS_VERSION}/Audiveris-${AUDIVERIS_VERSION}-ubuntu24.04-x86_64.deb" \
 && dpkg-deb -x /tmp/audiveris.deb / \
 && mkdir -p /opt/tessdata \
 && for lang in eng por; do \
      curl -fsSL -o /opt/tessdata/$lang.traineddata "https://github.com/tesseract-ocr/tessdata/raw/4.1.0/$lang.traineddata"; \
    done \
 && rm -f /tmp/audiveris.deb && rm -rf /var/lib/apt/lists/*

# pdftotext: the printed text of digital PDFs (title, "SOPRANO", ...) is read to identify voices.
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r /app/backend/requirements.txt
ENV PATH="/venv/bin:$PATH"

# Frontend libraries are vendored at build time so the app works offline.
ARG TONE_VERSION=15.0.4
ARG OSMD_VERSION=1.9.0
RUN mkdir -p /app/vendor \
 && curl -fsSL -o /app/vendor/Tone.js "https://cdn.jsdelivr.net/npm/tone@${TONE_VERSION}/build/Tone.js" \
 && curl -fsSL -o /app/vendor/opensheetmusicdisplay.min.js \
      "https://cdn.jsdelivr.net/npm/opensheetmusicdisplay@${OSMD_VERSION}/build/opensheetmusicdisplay.min.js"

COPY backend /app/backend
COPY frontend /app/frontend

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8000"]
