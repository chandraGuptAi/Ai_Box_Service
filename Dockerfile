# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENV=production \
    DEBUG=false \
    ATTACHMENT_STORAGE_DIR=/app/data/attachments \
    REPORT_MEMORY_DB=/app/data/report_memory.sqlite \
    HF_HOME=/app/data/huggingface

WORKDIR /app

# Local OCR with English and Hindi language packs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m pip check

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /app/data/attachments /app/data/huggingface \
    && chown -R app:app /app/data

COPY app ./app
COPY modules.yaml ./modules.yaml
# Attachment planning reads this skill at runtime.
COPY .agents/skills/attachment-report ./.agents/skills/attachment-report

USER app
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--timeout-graceful-shutdown", "30"]
