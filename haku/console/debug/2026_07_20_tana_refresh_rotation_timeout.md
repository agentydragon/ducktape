# Tana refresh rotation timeout

On 2026-07-20, `tana-rw` became available immediately after operator OAuth connection and then
became unavailable at its first refresh.

## Evidence

- Haku started the refresh with a fixed 10-second HTTP timeout and retained token revision 0 after
  `httpx.ReadTimeout`.
- Authentik completed the token request successfully in 11.432 seconds. Its logs show concurrent
  Terraform reconciliation API requests taking several seconds during the same interval.
- The Tana facade persisted and logged the rotated refresh token after Haku had stopped waiting.
- Every later Haku attempt replayed the consumed token, so the facade correctly returned
  `invalid_grant`. The Tana MCP backend itself remained healthy.

## Root cause

The refresh protocol rotates tokens but is not replay-safe after an ambiguous client timeout. Haku
discarded a successful late response, stored no durable failure state, and treated the old token as
safe to retry. Authentik control-plane contention made the normally quick request exceed Haku's
deadline; Tana execution latency was not involved.

## Resolution

- Persist a sanitized refresh-failure episode, including its initiating and latest failures.
- Stop replay after an ambiguous response timeout; require reconnection instead.
  **Partly superseded 2026-07-31**: requiring a reconnect after _every_ ambiguous timeout cost an
  association a manual reconnect for each transient one, and at a 10-minute access-token lifetime
  that was ~150 chances a day. An ambiguous timeout is now retryable and stops on the first
  definitive answer; unbounded replay, the actual failure here, is still prevented because
  `invalid_grant` is terminal. Authentik does not revoke the token family on reuse.
- Back off failures known to be retryable and make the remote MCP token timeout configurable.
- Remove the facade from Haku's Tana path: Haku Console holds the Tana PAT and calls the internal
  MCP service, while the public facade remains available for external clients.
