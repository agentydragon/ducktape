# Webhook Inbox

Tiny FastAPI-based service for catching arbitrary webhook calls during
development or troubleshooting. Incoming JSON / text payloads are stored in a
SQLite database and rendered via a bare-bones web UI.

## Quick start (local)

See `@AGENTS.md` in the repository root for Bazel build, test, and lint workflows.

```bash
# Generate an encryption key (optional but recommended)
export WEBHOOK_INBOX_KEY=$(python gen_key.py)

uvicorn webhook_inbox:app --reload
```

## Container deployment

```bash
# Build image (or pull from your registry)
docker build -t webhook-inbox:latest .

# Generate an encryption key (optional but recommended)
KEY=$(python gen_key.py)

# Persist database outside the container
mkdir -p $(pwd)/data

# Run
docker run -d --name inbox \
  -e WEBHOOK_INBOX_KEY="${KEY}" \
  -v $(pwd)/data:/data \
  -p 8000:8000 \
  webhook-inbox:latest

# Health-check
docker inspect --format='{{json .State.Health}}' inbox
```

Use Nginx (or Caddy, Traefik, …) in front of the container for HTTPS, e.g.

```nginx
location /hooks/ {
    proxy_pass       http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Configuration

All options are environment variables; sensible defaults are compiled in.

| Variable            | Purpose                                         |
| ------------------- | ----------------------------------------------- |
| `WEBHOOK_INBOX_KEY` | 44-char Fernet key for exporting encrypted logs |
| `DB_PATH`           | Path to SQLite file (default: `events.db`)      |
| `MAX_PAYLOAD`       | Bytes stored per request (default: 16384)       |
| `PAGE_SIZE`         | Events shown per UI page (default: 50)          |
| `TZ`                | IANA timezone for UI timestamps                 |
| `LOG_LEVEL`         | Python/uvicorn log level (default: INFO)        |

## Logging

Each request is:

1. Persisted in the `access_log` SQLite table (payload truncated to
   `MAX_PAYLOAD`).
2. Emitted as a single **stdout line** _without_ the request body so sensitive
   data does not leave the container. Example:

```
2024-05-15T12:34:56+0000 INFO webhook_inbox - handled_request {'method': 'POST', 'path': '/', 'query': '', 'status': 200}
```

## Health-check

The Docker image defines a `HEALTHCHECK` that performs a simple GET on `/`.
If it fails, Docker marks the container as _unhealthy_ so your supervisor can
restart it.
