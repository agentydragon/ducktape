# study_casino

Single-user habit-tracking "casino" — study for a session, earn credits,
gamble those credits in the casino for tokens, then spend tokens on
self-chosen prizes. Credits → tokens is one-way (via the casino or an
explicit conversion), so winnings can never be re-gambled. Frontend is
a React PWA installable on Windows (Edge/Chrome) and iPhone (Safari →
Add to Home Screen).

Lives at <https://casino.allegedly.works>.

## Layout

| Path                                   | What it is                                                                                         |
| -------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `app.py`                               | FastAPI backend: REST + thin WebSocket fan-out, static frontend                                    |
| `actions.py`                           | Pydantic schemas for server-authoritative action endpoints                                         |
| `config.py`                            | Pydantic settings (DATABASE_URL, ADMIN_USERS, host, port, OIDC)                                    |
| `models.py`                            | SQLAlchemy rows: balance, sessions, prizes, prize_log, ledger, snapshots, hands                    |
| `events.py`                            | Pydantic schemas for game and ledger event reads                                                   |
| `state.py`                             | Pydantic schemas for `GET /state` / `/me` / `/admin/users` / `/healthz` and the `/ws` payload      |
| `export_schema.py`                     | Schema-only FastAPI app — prints OpenAPI JSON for the Zod codegen below                            |
| `games.py`                             | Server-side slots, roulette, and blackjack rules/RNG                                               |
| `store.py`                             | `SqlStore`: idempotent server-action runner over Postgres                                          |
| `migrations/`                          | Alembic migrations applied at startup against the CNPG Postgres database                           |
| `conftest.py`                          | Postgres testcontainer fixture (per-test isolated database)                                        |
| `test_store.py`                        | SqlStore: idempotency, snapshots, validators                                                       |
| `test_app.py`                          | HTTP-surface coverage of every action + `/state` + `/ws`                                           |
| `tests/test_e2e_browser.py`            | Real-Playwright browser smoke (sync ok, state 5xx → offline)                                       |
| `frontend/study_casino.jsx`            | App shell: header, nav, offline banner, view routing                                               |
| `frontend/shared.jsx`                  | Shared constants (`COLORS`, `SUBJECTS`), components (`SectionTitle`, `StatCard`, `WinBurst`, etc.) |
| `frontend/StudyView.jsx`               | Study timer and session list                                                                       |
| `frontend/Roulette.jsx`                | Roulette game                                                                                      |
| `frontend/Blackjack.jsx`               | Blackjack game                                                                                     |
| `frontend/Slots.jsx`                   | Slot machine game                                                                                  |
| `frontend/PrizesView.jsx`              | Token conversion, prize catalog, redemption log                                                    |
| `frontend/StatsView.jsx`               | Stats, session editor, data import/export                                                          |
| `frontend/CasinoStatsView.jsx`         | Casino payout history — per-game, per-wager-type and per-day empirical vs theoretical              |
| `frontend/lib/BUILD.bazel`             | `js_openapi_zod` target — emits `lib/api/schema.zod.mjs` from `export_schema.py`'s OpenAPI doc     |
| `frontend/sync.js`                     | REST client + state-changed WebSocket subscriber (parses responses through generated Zod schemas)  |
| `frontend/use_casino.js`               | Single hook exposing reactive state + every mutation                                               |
| `frontend/SyncIcon.jsx`                | Header status icon + rejection toast                                                               |
| `frontend/main.jsx`                    | Entry — renders into `#root`, unregisters legacy service workers                                   |
| `frontend/index.html`                  | App shell (manifest link, theme color, apple-\* meta)                                              |
| `frontend/public/manifest.webmanifest` | PWA manifest                                                                                       |
| `frontend/public/sw.js`                | Kill-switch service worker (unregisters + clears caches)                                           |
| `frontend/public/icon.svg`             | App icon                                                                                           |
| `frontend/public/fonts/`               | Hermetic latin-subset fonts (Outfit, Playfair Display)                                             |
| `frontend/vite.config.js`              | Production bundler config (Vite + @vitejs/plugin-react)                                            |
| `BUILD.bazel` / `frontend/BUILD.bazel` | Bazel wiring                                                                                       |

## Auth

OIDC Authorization Code flow (confidential client). The backend (`auth.py`)
handles `/auth/login` → Authentik → `/auth/callback`, then issues an
HMAC-signed session cookie. Per-user state is scoped by `user_id` in the
shared Postgres database. Authentik resources (OAuth2 provider, application,
policy bindings) are managed by TF at
`tf/gitops/sso-providers/provider_study_casino.tf`.

Usernames listed in `STUDY_CASINO_ADMIN_USERS` (comma-separated) have
admin privileges: they can manage prize catalogs for other users via
`/admin/*` endpoints. Non-admin users cannot create or delete prizes,
even their own — admins curate the catalog. Anyone can still redeem
prizes against their own token balance.

## State

Canonical state is a small relational schema in Postgres (CNPG `study-casino-db`),
shared-schema with rows scoped by `user_id`:

| Table               | Purpose                                                                         |
| ------------------- | ------------------------------------------------------------------------------- |
| `balance`           | Singleton row (`id = 1`); credits, tokens. CHECK constraints enforce `≥ 0`.     |
| `sessions`          | One row per completed study session. In-progress sessions are client-side only. |
| `prizes`            | User-editable prize catalog.                                                    |
| `prize_log`         | Append-only redemption log.                                                     |
| `ledger_events`     | Append-only audit trail of every server action, keyed by `client_action_id`.    |
| `game_events`       | Server-resolved slots/roulette/blackjack settlements.                           |
| `rng_action_audits` | Deterministic RNG seed context per randomized server action.                    |
| `rng_call_audits`   | Per-call RNG parameters/results for replaying randomized server actions.        |
| `state_snapshots`   | JSON dumps before `/actions/import` / `/actions/reset`.                         |
| `blackjack_hands`   | In-flight hand state between deal and settlement.                               |

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
GET  /casino/stats                   — aggregated wager/payout stats for caller
GET  /admin/casino/stats?user=<u>    — admin-only: aggregated stats for any user
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

Casino RNG is deterministic and audit-logged for new server-resolved actions.
The DB stores seed material plus each random call's parameters and result; the
versioned `STUDY_CASINO_RNG_SECRET` stays outside the DB and is needed to replay
the HMAC-SHA256 stream.

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
bbr test //x/study_casino/...  //x/study_casino/tests/...
bbr build //x/study_casino:image
```

Local dev:

```bash
# Iterate on the frontend with Vite's HMR dev server:
cd x/study_casino/frontend && pnpm exec vite

# Or build the production bundle once (matches what Bazel produces):
cd x/study_casino/frontend && pnpm exec vite build

# Run the backend against the local dist:
bb run //x/study_casino:server
```
