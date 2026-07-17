# Haku Google access: console mediation and Airlock decoupling

How Haku reaches Google (Gmail, Calendar, Drive, Tasks) and how that access moves off Airlock
onto haku-console. This is a Haku credential-architecture plan; the cross-cutting OAuth/identity
program that contains it lives in `plans/oauth_architecture.md`.

Sequenced later than the common Agent lifecycle (H1–H3 there).

## Progress

1. **G1 (done):** the console owns the per-Operator Google connection —
   `haku/console/provider_connection.py` (Postgres per-Operator refresh storage + in-process
   self-refresh), the `/api/provider-connections/*` connect/status/disconnect flow, the
   `provider_connection: google` marker with execution-time Operator selection, and the Settings →
   Connected accounts UI. The console holds its own dedicated Google OAuth client
   (`haku-console-google-client-credentials`, project `rai-personal`, independent of Airlock's). A
   downstream-provider relationship, not Agent enrollment or an Agent-held credential.
2. **G2 (done):** removed `haku_console_google`, its Secret publication/External Secrets mirror, and
   its airlock-side producer (#3364). The console-owned token never reaches an Agent — it lives only
   in the `haku-console` Postgres, and Agents reach Gmail/Calendar solely through the console's
   approval-gated MCP tools.
3. **G3 (later — not scheduled):** retire Haku's _last_ Airlock dependency, the read-only
   `google-access-token` (`$TOK`) that the `google` Airlock grant reflects into `haku-sandbox`.
   Today the agent holds it directly for Drive/Tasks and as the Gmail/Calendar REST fallback
   (`haku/base/sources/`). What replaces it is the target below.

Do not couple G1/G2/G3 to Airlock's unrelated Oura, BSC, or remaining credential consumers.

## Target: console mediates all Google access, agent holds no standing token

The clean end state: **haku-console holds the Google client(s) and mediates every Google operation;
no Google token with standing capability ever reaches the agent.**

- **High-risk operations — invariant, not a preference.** Anything the operator does not want Haku
  to execute autonomously (sending mail, deleting/modifying Drive files, mutating calendars, …) runs
  only through haku-console tools behind its approval policy. A token carrying those permissions must
  never be handed to the agent. This already holds for Gmail/Calendar writes.
- **Low-risk (read-only) — a genuine tradeoff, currently unresolved:**
  - _Direct token (status quo):_ the agent holds the read-only `$TOK`. Simpler, but it is a standing
    bearer secret in agent context — if it leaks through the LLM provider, whoever reads it can read
    all of the operator's mail/Drive going forward, bounded only by rotation cadence.
  - _Console-mediated (cleaner, more secure):_ route reads through console MCP tools too, so the
    agent holds no Google token at all. Cost: implementing a potentially large read tool surface
    (Drive, Tasks, remaining Gmail/Calendar read affordances) — which may be worth doing anyway, and
    would let G3 drop the `google` Airlock grant and the `haku-sandbox` reflection entirely.

  Leaning console-mediated for the security win; decision deferred. Until then the read-only token
  stays (least-privilege by construction — all `.readonly` scopes).
