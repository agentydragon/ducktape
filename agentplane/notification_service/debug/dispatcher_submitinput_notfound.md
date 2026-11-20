# Dispatcher SubmitInput NotFound retries — 2026-11-18

## Observation

After [#17416](https://github.com/agentydragon/ducktape/pull/17416) rolled out, both Notifications replicas continued to log `ServiceError: NotFound` for one inbox. The source is inferred from the cause name and the #17416 logging/code paths: NotFound from Dispatcher `GetSession` or History reads is logged as `SessionNotFoundError` or `HistoryNotFoundError`, while the plain `ServiceError: NotFound` corresponds to the plain `unavailable(err)` around `SubmitInput`. No read observed the `GetSession` or History results. **#17416 did not cover the live source**; its longer retry covered only NotFound from `GetSession` and `ReadSessionEvents`.

## Dispatcher path

`GetSession` reads the retained Session row and does not consult live Sandbox inventory. `SubmitInput` calls `enqueue`, which reads the Session, then `commandTarget` resolves the Session's stored Sandbox name through the Dispatcher's live Sandbox inventory before it admits the command. An absent Sandbox returns NotFound. When the Sandbox exists, caller/target mismatches return other status codes; the placement-specific central/native route is selected later by the delivery worker. An absent optional environment attachment is therefore not itself the source of this NotFound. Current central sampler V1 placement still binds a required executor Sandbox.

For a stable Session row and the configured Notifications service caller, this code path makes the Session's exact live Sandbox target the relevant live lookup. A retained Session can still be returned by `GetSession` after its Sandbox was deleted. A concurrent Session-row cleanup between the two Dispatcher reads is another possible NotFound path. The current investigation identity cannot read the other owner's inbox row or Sandbox, so it has not confirmed the target's name, UID, or `GetSession.status.required_environment` value. Do not report those row-specific facts as observed until an authorized read establishes them.

## Retry path

`unavailableNotFound` assigns `unavailable_destination_retry_s` (30 seconds by default) to the earlier `GetSession` and History NotFound cases. `SubmitInput` used plain `unavailable`, leaving `retryAfter` at zero, so `Worker.release` selected the five-second minimum. `Store.ReleaseWithRetry` correctly clamps `noticeDueAt` to that minimum when delivery failed. However, `Store.Send` unconditionally advanced `inbox.next_attempt` to now for each new entry, which could erase an already-recorded failure deadline before its retry.

The fix classifies `SubmitInput` NotFound separately and carries the configured destination retry. New entries preserve an existing non-null retry deadline while `delivery_error` is recorded; when no delivery retry is scheduled, they still wake the worker immediately so it can calculate the debounce deadline. The deterministic tests cover both paths without waiting on timers.

## Evidence limits

Read-only logs established the repeated plain `ServiceError: NotFound` signature. The attribution to `SubmitInput` is an inference from that cause name and the #17416 logging/code paths; no read observed the `GetSession` or History results. Source inspection establishes the possible Dispatcher NotFound sites and the retry behavior. This investigation's current Identity could read pod logs but was denied Sandbox reads and could not read the target inbox through the caller-scoped Notification API. The target Sandbox's absence remains unverified; no staging data was read or changed through a database or alternate identity.
