FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps. libpq isn't needed since asyncpg is pure python on top of libssl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first to leverage Docker layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "sqlalchemy[asyncio]>=2.0" \
        "asyncpg>=0.30" \
        "alembic>=1.13" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "jinja2>=3.1" \
        "python-multipart>=0.0.12" \
        "itsdangerous>=2.2" \
        "httpx>=0.27" \
        "structlog>=24.4" \
        "sentry-sdk[fastapi]>=2.18" \
        "email-validator>=2.2"

COPY . .

# Non-root user.
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
