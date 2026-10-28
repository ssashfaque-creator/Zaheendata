FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ZAHEEN_DATA_DIR=/data \
    ZAHEEN_HTTP_CACHE_PATH=/data/http_cache.sqlite \
    ZAHEEN_CONFIG_DIR=/app/config

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 libxslt1.1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip && pip install ".[postgres,redis]"

RUN useradd --create-home --uid 10001 zaheen && mkdir -p /data && chown -R zaheen /data /app
USER zaheen
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["zaheen", "serve"]
