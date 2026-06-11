# Study Casino Server-Side Resolution Notes

## Implemented Shape

As of 2026-05-06, the first server-authority pass is implemented but should
remain deployed in observe mode until live logs prove that current clients are
using the new action endpoints.

1. Keep the Y.Doc for replicated user-facing state: sessions, balances, prizes,
   and prize redemptions.
2. Move wager settlement to authenticated backend endpoints. A request says
   "spin slots for 5 credits" or "bet 10 credits on red"; the server checks the
   canonical balance, draws the outcome, writes `game_events`, updates the
   canonical Y.Doc balance, and returns the outcome for animation.
3. Use server-side randomness with an explicit `rng_version` in each event. If
   we want user-auditable fairness later, add a commit/reveal seed chain or
   public daily server seed hash.
4. Convert games in risk order:
   - Slots first: one request, one response, no player decisions.
   - Roulette next: one request, one response, simple bet validation.
   - Blackjack last: needs a short-lived server hand state plus hit, stand,
     double, and settle actions.
5. Once a game is server-resolved, mark its events with
   `source = "server_resolved"` and stop accepting client-reported settle events
   for that game.

Implemented details:

- Server action endpoints now own balance-changing study sessions, conversion,
  prize redemption, import/reset, slots, roulette, and blackjack flows.
- `ledger_events` records every accepted or observed economy-changing action.
- `game_events` records server-resolved casino settlements with `rules_version`
  and `rng_version`.
- `state_snapshots` stores raw Y.Doc snapshots before import/reset and initial
  authority adoption.
- `blackjack_hands` stores active hand state so hit/stand/double decisions stay
  server-side.
- `STUDY_CASINO_AUTHORITY_MODE=observe` keeps stale-client detection visible
  without rejecting existing clients yet; `enforce` rejects legacy
  client-reported game events and direct client `balance`/`prize_log` sync.

## Next Live Notes

After the CI-built image deploys, record:

1. Git commit, image tag, Flux `ImagePolicy` latest tag, and live pod image.
2. Pod name, ready status, restart count, and whether startup migrations
   completed cleanly.
3. For each live SQLite database under `/data`, `alembic_version` and row counts
   for `game_events`, `ledger_events`, `state_snapshots`, and `blackjack_hands`.
4. A small current-UI smoke flow, ideally one casino action, with before/after
   counts showing one new `ledger_events` row and one new
   `game_events.source = "server_resolved"` row.
5. Whether any `ledger_events.action_type = "legacy_client_sync"` rows appeared
   after the new frontend was deployed.

Only flip `STUDY_CASINO_AUTHORITY_MODE` to `enforce` after those notes show that
current clients no longer use legacy casino settlement paths. Only plan CRDT
removal after PVC backups are restore-tested and the Y.Doc projection can be
reconstructed from server logs or snapshots without state loss.
