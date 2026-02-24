# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/adgn

RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt/lists \
    apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip pip install --upgrade pip \
    && pip install .

EXPOSE 8000 8100

ENTRYPOINT ["python", "-m", "adgn.rspcache.cli", "run"]
CMD ["--app", "proxy", "--host", "0.0.0.0", "--port", "8000"]
