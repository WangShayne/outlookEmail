# syntax=docker/dockerfile:1

############################
# Builder
############################
FROM python:3.11-slim AS builder

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Build dependencies (minimal)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    && pip wheel --no-cache-dir --wheel-dir /wheels gunicorn

############################
# Runtime
############################
FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PORT=5001 \
    GUNICORN_WORKERS=2 \
    GUNICORN_TIMEOUT=120

# Runtime dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN addgroup --system app \
    && adduser --system --ingroup app --home /app app

COPY --from=builder /wheels /wheels
COPY requirements.txt .

RUN pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels

COPY . .

# Create data directory and entrypoint to fix volume permissions
RUN mkdir -p /app/data \
    && chown -R app:app /app \
    && printf '#!/bin/sh\\nset -e\\nchown -R app:app /app/data\\nexec gosu app \"$@\"\\n' > /entrypoint.sh \
    && chmod +x /entrypoint.sh

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:${APP_PORT}/login || exit 1

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS} -b 0.0.0.0:${APP_PORT} --timeout ${GUNICORN_TIMEOUT} --access-logfile - web_outlook_app:app"]
