# Webhook Inbox

Tiny FastAPI service for catching arbitrary webhook calls. Payloads stored in SQLite, rendered via web UI.

## Quick start

```bash
export WEBHOOK_INBOX_KEY=$(python gen_key.py)  # Optional Fernet encryption key
uvicorn webhook_inbox:app --reload
```

## Configuration

| Variable            | Purpose                                     | Default     |
| ------------------- | ------------------------------------------- | ----------- |
| `WEBHOOK_INBOX_KEY` | 44-char Fernet key for encrypted log export |             |
| `DB_PATH`           | SQLite file path                            | `events.db` |
| `MAX_PAYLOAD`       | Bytes stored per request                    | `16384`     |
| `PAGE_SIZE`         | Events per UI page                          | `50`        |
| `TZ`                | IANA timezone for UI timestamps             |             |
| `LOG_LEVEL`         | Log level                                   | `INFO`      |

Request bodies are **not** included in stdout logs (only method, path, status). Docker image includes a `HEALTHCHECK` on `/`.
