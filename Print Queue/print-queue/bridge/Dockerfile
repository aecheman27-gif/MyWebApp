FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /bridge

RUN pip install --upgrade pip && \
    pip install \
        "paho-mqtt>=2.1" \
        "httpx>=0.27" \
        "structlog>=24.4"

COPY bridge/ /bridge/

RUN useradd --create-home --shell /bin/bash bridge \
    && mkdir -p /data \
    && chown -R bridge:bridge /bridge /data
USER bridge

CMD ["python", "-m", "bridge.main"]
