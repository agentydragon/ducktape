# Gatelet

Service that lets LLMs access real-time and historical information relevant to the user, providing a browsable interface focused on Home Assistant integration.

### Core Components

1. **Server** - FastAPI-based web service that:
   - Receives and stores webhooks in PostgreSQL
   - Provides browsable interface optimized for LLMs
   - Retrieves and presents Home Assistant data
   - Offers multiple authentication methods
   - Includes admin interface for humans

2. **Reporter** - Single Python program `gatelet-reporter` that:
   - Sends events to the server
   - Can run as a long-running daemon for tasks like battery reporting
   - Intended to be installed on laptops and other devices

### Reporter Usage

Send an arbitrary JSON payload:

```bash
gatelet-reporter event --url http://localhost:8000 \
  --integration laptop '{"foo": "bar"}'
```

Run the daemon according to your configuration:

```bash
gatelet-reporter
```

## Development Setup

The project requires Python 3.10+ and a PostgreSQL database. Two development approaches are available:

### Option 1: Docker Compose (Recommended)

The easiest way to start developing is using Docker Compose with the included development environment:

```bash
# Install development dependencies (includes invoke task runner)
pip install -e '.[dev]'

# Copy example configuration
cp gatelet.example.toml gatelet.toml
# Edit gatelet.toml with your API keys

# Start everything with one command
invoke setup
```

This starts PostgreSQL and Gatelet with automatic code reloading. The service will be available at <http://localhost:8000>.

Available development commands:

- `invoke up` - Start development environment with live reload
- `invoke down` - Stop all services
- `invoke test` - Run tests
- `invoke shell` - Open shell in container
- `invoke db` - Connect to PostgreSQL
- `invoke format` - Format code with black/isort
- `invoke --list` - Show all available commands

### Option 2: Manual Setup

If you prefer to run services directly:

1. Start PostgreSQL in Docker:

```bash
docker run --name gatelet-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=gatelet -p 5432:5432 -d postgres:16
```

2. Create a virtual environment and install Gatelet with development dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

3. Copy the example configuration and set the database URL:

```bash
cp gatelet.example.toml gatelet.toml
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/gatelet
```

Edit `gatelet.toml` and set `home_assistant.api_url` to your Home Assistant
instance. Admin pages will link back to this URL.

4. Initialize the database and start the server:

```bash
alembic upgrade head
uvicorn gatelet.server.app:app --reload --host 0.0.0.0 --port 8000
```

The service will be available at <http://localhost:8000>. When finished, stop the database container with:

```bash
docker stop gatelet-db
docker rm gatelet-db
```

For development inside the Codex devcontainer, run `gatelet/setup.sh` from the repository root before network access is disabled.

### Administration

Common management tasks are wrapped in a small `Makefile`:

```bash
make -C gatelet reset-db        # initialize a fresh database
make -C gatelet change-password # change the admin password
```

`reset-db` displays the current row counts for all tables and asks for confirmation before dropping everything. It then creates a fresh admin account with password `gatelet`.

### Testing and Development

Install the project with development dependencies and run tests using `pytest`.
When `IS_CODEX_ENV=1` is set, the test suite automatically launches a temporary
PostgreSQL server and removes it after the tests finish.

```bash
pip install -e '.[dev]'
pytest gatelet
```

Before committing, run:

```bash
pre-commit run --files <changed files>
```

## LLM-Friendly Design

Designed for current LLM constraints (as of May 2025), particularly OpenAI scheduled tasks with o3 model:

- Navigation entirely link-based (no forms, inputs, or JavaScript)
- Authentication via URL paths or challenge-response
- All functionality accessible via GET requests
- Self-describing interfaces guide LLMs on service usage
- Each HTML template starts with a comment indicating its audience
  (human admin, LLM, or both). Pages for LLMs provide only link-based
  navigation without forms.

### OpenAI o3 Model Constraints

- Can execute Python code but cannot access URLs computed in Python
- Can only navigate to URLs explicitly given by users or links from pages
- Cannot use cookies or maintain browser state between page loads
- Cannot execute JavaScript or submit forms

## Authentication Methods

Gatelet supports multiple authentication methods:

1. **Key in Path** - Simple authentication by including key in URL path
   - Usage model: User provides direct URL with embedded key (<http://server/k/SECRET_KEY/>)
   - Example: `/k/{key}/`

2. **Challenge-Response** - Secure authentication using nonce challenges
   - Usage model: User provides base URL and secret key separately
   - LLM visits base URL, receives challenge, computes answer with Python
   - Server presents multiple link options (no URL computation needed)
   - LLM selects correct link from options based on computation
   - Incorrect selection invalidates the challenge
   - Success grants session with time-limited links

3. **Human Admin Authentication** - Standard username/password for human administrators
   - Uses cookies for **admin session** management
   - Provides access to logs, session management for both **admin** and **LLM** sessions, and key administration

## Authentication and Session Terms

- **Pre-Shared Key (PSK)**: Secret value known to both server and LLM, never transmitted directly
- **Challenge**: Unique problem requiring PSK to solve, regenerated for each authentication attempt
- **Nonce**: Single-use random value ensuring challenges can't be replayed
  - Includes embedded timestamp to ensure freshness
  - Server tracks used nonces to prevent replay attacks
  - Server rejects nonces older than a configured time window
- **Session**: Authenticated period allowing access to protected resources
  - **Session Token**: Unique identifier embedded in page links
  - **Session Extension**: Every link clicked extends session by 5 minutes
  - **Session Duration Cap**: Maximum 1-hour lifetime even with continuous use
  - **Session Expiration**: Occurs after 5 minutes of inactivity

## Features

### Webhooks

- Receive and store webhooks from various sources
- View webhook history with pagination
- Optional encryption for sensitive data

### Home Assistant Integration

- Current state of configured entities with friendly names
- Historical state changes for discrete entities
- Trend data for continuous sensors (temperature, humidity, etc.)
- Direct links back to Home Assistant when viewed by a human admin

### Session Management

- Challenge-based authentication for LLMs (LLM sessions)
- Time-limited tokens with automatic extension
- Human admin interface for viewing **admin sessions**
  and **LLM sessions**, managing keys, and monitoring logs

## Implementation Plan

The project is implemented in phases:

1. **Phase 1** – Webhooks with Key‑in‑Path Authentication _(completed)_
   - Basic FastAPI server and PostgreSQL schema
   - Webhook receiving and storage
   - Key‑in‑path authentication

2. **Phase 2** – Challenge‑Response Authentication _(completed)_
   - Nonce‑based login flow for LLMs
   - Session management with automatic extension

3. **Phase 3** – Home Assistant Integration _(in progress)_
   - Basic entity state listing implemented
   - Historical and trend views pending

4. **Phase 4** – Human Admin Interface _(in progress)_
   - Password‑based admin login implemented
   - Key management pages available
   - Session management implemented
   - Log inspection page available

## Current Status

Gatelet runs with both key‑in‑path and challenge‑response authentication.
Webhooks can be received and browsed. The admin login, key management,
session overview, and log pages are operational. Home Assistant entity states
list friendly names and admins get direct links back to the Home Assistant UI.
History and trend views are still pending.
See `gatelet/TODO.md` for the remaining tasks.
