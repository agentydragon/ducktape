# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/emberd

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl jq git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY docs ./docs

RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --upgrade pip \
    && python3 -m pip install --no-cache-dir .

EXPOSE 8000

ENTRYPOINT ["emberd"]
