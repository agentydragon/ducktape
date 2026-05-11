# auragon_study_casino

Single-user habit-tracking "casino" — study for a session, earn credits,
gamble those credits in the casino for tokens, then spend tokens on
self-chosen prizes. Credits → tokens is one-way (via the casino or an
explicit conversion), so winnings can never be re-gambled. Frontend is
a React PWA installable on Windows (Edge/Chrome) and iPhone (Safari →
Add to Home Screen).

Lives at <https://casino.allegedly.works>.

## Layout

| Path                                   | What it is                                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------- |
| `app.py`                               | FastAPI backend: REST + thin WebSocket fan-out, static frontend                 |
| `actions.py`                           | Pydantic schemas for server-authoritative action endpoints                      |
| `config.py`                            | Pydantic settings (DATA_DIR, host, port)                                        |
| `models.py`                            | SQLAlchemy rows: balance, sessions, prizes, prize_log, ledger, snapshots, hands |
| `events.py`                            | Pydantic schemas for game and ledger event reads                                |
| `games.py`                             | Server-side slots, roulette, and blackjack rules/RNG                            |
| `store.py`                             | `SqlStore`: idempotent server-action runner over SQLite                         |
| `migrations/`                          | Alembic migrations for the per-user SQLite database                             |
| `migrations/test_0004_backfill.py`     | Round-trip test for the irreversible Y.Doc → relational backfill                |
| `test_store.py`                        | SqlStore: idempotency, snapshots, validators                                    |
| `test_app.py`                          | HTTP-surface coverage of every action + `/state` + `/ws`                        |
| `tests/test_e2e_browser.py`            | Real-Playwright browser smoke (sync ok, state 5xx → offline)                    |
| `frontend/src/study_casino.jsx`        | The React component (originally a claude.ai artifact)                           |
| `frontend/src/sync.js`                 | REST client + state-changed WebSocket subscriber                                |
| `frontend/src/use_casino.js`           | Single hook exposing reactive state + every mutation                            |
| `frontend/src/SyncIcon.jsx`            | Header status icon + rejection toast                                            |
| `frontend/src/main.jsx`                | Entry — renders into `#root`, unregisters legacy service workers                |
| `frontend/index.html`                  | App shell (manifest link, theme color, apple-\* meta)                           |
| `frontend/public/manifest.webmanifest` | PWA manifest                                                                    |
| `frontend/public/sw.js`                | Kill-switch service worker (unregisters + clears caches)                        |
| `frontend/public/icon.svg`             | App icon                                                                        |
| `frontend/public/fonts/`               | Hermetic latin-subset fonts (Outfit, Playfair Display)                          |
| `frontend/vite.config.js`              | Production bundler config (Vite + @vitejs/plugin-react)                         |
| `BUILD.bazel` / `frontend/BUILD.bazel` | Bazel wiring                                                                    |

## Auth

OIDC Authorization Code flow (confidential client). The backend (`auth.py`)
handles `/auth/login` → Authentik → `/auth/callback`, then issues an
HMAC-signed session cookie. Per-user SQLite databases scope state to the
authenticated user. Authentik resources (OAuth2 provider, application, policy
bindings) are managed by TF at
`tf/gitops/sso-providers/provider_study_casino.tf`.

## State

Canonical state is a small relational schema in per-user SQLite:

| Table             | Purpose                                                                         |
| ----------------- | ------------------------------------------------------------------------------- |
| `balance`         | Singleton row (`id = 1`); credits, tokens. CHECK constraints enforce `≥ 0`.     |
| `sessions`        | One row per completed study session. In-progress sessions are client-side only. |
| `prizes`          | User-editable prize catalog.                                                    |
| `prize_log`       | Append-only redemption log.                                                     |
| `ledger_events`   | Append-only audit trail of every server action, keyed by `client_action_id`.    |
| `game_events`     | Server-resolved slots/roulette/blackjack settlements.                           |
| `state_snapshots` | JSON dumps before `/actions/import` / `/actions/reset`.                         |
| `blackjack_hands` | In-flight hand state between deal and settlement.                               |

`GET /state` returns a JSON view of `balance` + `sessions` + `prizes` +
`prize_log`. The frontend caches this and refetches on every successful
action and on every WebSocket `state_changed` ping.

## Wire surface

```
GET  /state                          → full canonical state JSON
POST /actions/session/complete       — commit an active session (client supplies timing)
POST /actions/session/add-past       — backfill a past session
POST /actions/session/edit           — rename / re-time a completed session
POST /actions/session/delete         — drop a completed session
POST /actions/convert                — credits → tokens
POST /actions/prize/{create,delete}  — manage the prize catalog
POST /actions/prize/redeem           — spend tokens on a prize
POST /actions/import / /actions/reset — bulk replace / wipe state (snapshot saved)
POST /casino/slots/spin              — server-resolved slots
POST /casino/roulette/spin           — server-resolved roulette
POST /casino/blackjack/{deal,hit,stand,double} — server-resolved blackjack
GET  /game-events / /ledger-events   — read-only audit listings
GET  /me / /healthz                  — auth introspection / liveness
WS   /ws                             — broadcasts {"type":"state_changed"} to every
                                        tab of the same user after a successful action
```

The active study-session timer (start / pause / resume / cancel) lives in
client `localStorage`; the server only learns about it when the user calls
`/actions/session/complete`. Every action carries a `client_action_id` —
retried calls return the original `ledger_events` row without replaying
the mutation.

`game_events` is the queryable casino history. Pre-2026-05-07 rows have
`source="client_reported"` (legacy direct settlements) and
`source="server_resolved"` rows are written from this point forward.
Pre-cutover `ledger_events` rows with `source="legacy_client_sync"`
similarly remain readable; both sets of literals are kept in `events.py`
so historical rows still deserialize.

## Validation

DB CHECK constraints + Pydantic field validators police the rules:
`balance.credits ≥ 0`, `balance.tokens ≥ 0`, `prizes.cost > 0`,
`sessions.subject` non-empty, etc. A mutator that would violate a CHECK
raises an SQLAlchemy `IntegrityError` at commit; the surrounding
`run_server_action` transaction rolls back. A mutator that explicitly
raises `ActionRejectedError("rule", "message")` produces a 409 with the
structured detail (e.g., `rule="insufficient_credits"`).

## Build

```bash
bbr test //x/auragon_study_casino/...  //x/auragon_study_casino/tests/...
bbr build //x/auragon_study_casino:image
```

Local dev:

```bash
# Iterate on the frontend with Vite's HMR dev server:
cd x/auragon_study_casino/frontend && pnpm exec vite

# Or build the production bundle once (matches what Bazel produces):
cd x/auragon_study_casino/frontend && pnpm exec vite build

# Run the backend against the local dist:
bb run --remote_executor="" //x/auragon_study_casino:server
```
