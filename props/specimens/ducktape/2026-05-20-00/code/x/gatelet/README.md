# Gatelet

Service that lets LLMs access real-time and historical information relevant to the user, providing a browsable interface focused on Home Assistant integration.

### Core Components

1. **Server** - FastAPI web service: webhooks (PostgreSQL), Home Assistant data, browsable LLM-optimized interface, admin UI
2. **Reporter** - `gatelet-reporter` daemon that sends device events to the server

### Reporter Usage

```bash
gatelet-reporter event --url http://localhost:8000 \
  --integration laptop '{"foo": "bar"}'
gatelet-reporter  # Run as daemon per config
```

## Development Setup

Requires PostgreSQL.

```bash
cp gatelet.example.toml gatelet.toml  # Edit with your API keys
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/gatelet
bazel run //x/gatelet/server:server
```

The server waits for PostgreSQL on startup and runs alembic migrations automatically.
Set `home_assistant.api_url` in `gatelet.toml` to your HA instance.

### Container Image

```bash
bazel build //x/gatelet:image   # Build OCI image
bazel run //x/gatelet:load      # Load into local Docker
```

### Administration

```bash
bazel run //x/gatelet:manage -- reset-db        # Fresh database
bazel run //x/gatelet:manage -- change-password  # Change admin password
```

## LLM-Friendly Design

All pages are link-based only (no forms, JS, cookies). Designed for LLM constraints:
LLMs can only follow explicit links, cannot compute URLs, and cannot maintain browser state.

## Authentication Methods

1. **Key in Path** - key embedded in URL: `/k/{key}/`
2. **Challenge-Response** - nonce-based; LLM selects correct link from options
3. **Human Admin** - username/password with cookie sessions

Session details: 5-min inactivity timeout, 1-hour max lifetime, link clicks extend by 5 minutes.

## Features

- **Webhooks**: receive, store, browse with pagination; optional encryption
- **Home Assistant**: entity states with friendly names, discrete state history, continuous sensor trends, admin links to HA UI
- **Session management**: admin interface for LLM and admin sessions, keys, logs

## Status

Phases 1-2 complete (webhooks, key-in-path, challenge-response auth). Phases 3-4 in progress (HA integration, admin UI). See <TODO.md> for remaining work.
