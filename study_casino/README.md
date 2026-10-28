# study_casino

Single-user habit-tracking "casino": study to earn credits, gamble credits for tokens, spend
tokens on self-chosen prizes. Credits → tokens is one-way (casino or explicit conversion), so
winnings are never re-gambled. React PWA installable on Windows (Edge/Chrome) and iPhone
(Safari → Add to Home Screen). Lives at <https://casino.allegedly.works>.

Staged API change: [action result discriminator rollout](docs/action_result_discriminators.md).

## Layout

- `app.py`: FastAPI REST + thin WebSocket fan-out, serves the frontend
- `store.py`: `SqlStore`, idempotent server-action runner over Postgres
- `games.py` / `rng.py`: server-side slots, roulette, blackjack rules and audited RNG
- `credit_award.py`, `credit_constants.py`: millicredit accounting, streak/daily bonus (credit
  system v2) and its tunables
- `state.py` / `actions.py` / `events.py`: wire models (reads, action results, event reads)
- `export_schema.py`: schema-only app printing OpenAPI to `:frontend_openapi_json`; the frontend
  compiles `//study_casino/frontend/lib:schema_zod_codegen` into
  `//study_casino/frontend/lib:schema_zod` (`schema.zod.mjs` and `.d.mts`)
- `migrations/`: Alembic, applied at startup against CNPG
- `frontend/`: `sync.js` (REST + WebSocket client, Zod-parsed), `use_casino.js` (state +
  mutations hook), one `.jsx` per view/game; bundled via `//devinfra/js:spa_bundle`.
  `public/sw.js` is a kill-switch service worker; `main.jsx` unregisters legacy ones.

## Auth

OIDC Authorization Code (confidential client) in `auth.py`: `/auth/login` → Authentik →
`/auth/callback` → HMAC-signed session cookie. Configure `STUDY_CASINO_OIDC_ISSUER`,
`STUDY_CASINO_OIDC_CLIENT_ID`, `STUDY_CASINO_OIDC_CLIENT_SECRET`, and
`STUDY_CASINO_SESSION_SECRET` together; empty or whitespace-only values are invalid, and a
partial configuration fails settings validation before the app serves requests. With all four
unset, the app accepts requests as user `default` for local development and tests. Authentik
resources: `tf/gitops/sso-providers/provider_study_casino.tf`. Users in
`STUDY_CASINO_ADMIN_USERS` (comma-separated) manage everyone's prize catalogs via `/admin/*`;
non-admins cannot create or delete prizes, even their own, but redeem against their own tokens.

## State

Postgres (CNPG `study-casino-db`), one shared schema with rows scoped by `user_id`. Credits are
integer **millicredits** (×1000) in every column and wire field (`*_millis`); tokens and wagers
are whole integers.

- `balance`: credits, tokens per user (CHECK `≥ 0`)
- `credit_state`: streak + daily bonus (Pacific days, append-only)
- `sessions`: completed study sessions; in-progress ones exist only client-side
- `prizes`, `prize_log` (append-only redemptions)
- `ledger_events`: append-only audit of every server action, keyed by `client_action_id`
- `game_events`: casino settlements
- `rng_action_audits`, `rng_call_audits`: seed context and per-call parameters/results for replay
- `state_snapshots`: dumps taken before `/actions/import` and `/actions/reset`
- `blackjack_hands`: in-flight hands between deal and settlement

## Wire surface

`GET /state` (balance, sessions, prizes, prize log) is cached by the frontend and refetched
after each successful action and on each `/ws` `{"type":"state_changed"}` broadcast (sent to all
of the user's tabs). Mutations are `POST /actions/*` (sessions, convert, prizes, changelog ack,
import/reset) and `POST /casino/{slots,roulette,blackjack}/*`; reads include `/casino/stats`,
`/admin/casino/stats?user=`, `/game-events`, `/ledger-events`, `/me`, `/healthz`.

- The study timer lives in client `localStorage`; the server sees a session only at
  `/actions/session/complete`.
- Every action carries a `client_action_id`; a retry returns the original `ledger_events` row
  without replaying the mutation.
- Historical rows keep their literals: `game_events` before 2026-05-07 have
  `source="client_reported"` (`server_resolved` after), pre-cutover `ledger_events` have
  `source="legacy_client_sync"`; both stay in `events.py` so old rows deserialize.
- Casino RNG is a deterministic HMAC-SHA256 stream; the DB stores seed material and each call's
  parameters/result, while the versioned `STUDY_CASINO_RNG_SECRET` stays outside it and is
  needed to replay.

## Validation

DB CHECKs and Pydantic validators enforce rules (`balance.credits/tokens ≥ 0`,
`prizes.cost > 0`, non-empty `sessions.subject`, …). A CHECK violation raises `IntegrityError` at
commit and the `run_server_action` transaction rolls back. `ActionRejectedError("rule", "msg")`
yields a 409 with structured detail (e.g. `rule="insufficient_credits"`).

## Build

```bash
bbr test //study_casino/...
bbr build //study_casino:image
bbr build //study_casino/frontend:bundle && bb run //study_casino:server   # local dev
```
