# Two binaries: Sessions and Environments

The operator adopted two binaries in place of `runner-gateway` and `sandbox-controller` on
2026-11-14 (05:29 UTC, card "Adopt it"); the Runner Gateway disappears.

- **Environments** ([`environments/`](../environments/README.md)): the Sandbox Controller,
  EnvironmentService, the Runtime proxy and the Executor. It owns Sandboxes and every call into an
  Environment Runtime, explicit resume, and keep-alive holds once they exist.
- **Sessions** ([`session_dispatcher/`](../session_dispatcher/README.md)): the Dispatcher. It owns
  Session admission, the durable command queue and routing, to the central sampler or through
  Environments to hosted harnesses, and automatic wake (05:19 UTC, card "Sessions binary wakes").

## Decisions

- **No call goes from Environments to Sessions.** Sessions re-check the live Identity themselves
  (07:20 UTC: "okay, sounds good to me, sessions can re-check"), and explicit resume reserves no
  wake budget (07:41 UTC, card "No budget for explicit resume"). Automatic wake and explicit resume
  share `environments/lifecycle` policy and guarded writes, with independent policy views.
- **Directory names** `session_dispatcher` and `environments` (05:49 UTC, card
  "session_dispatcher + environments"). The deployed Services are `sessions` and
  `environments`, with the server SVIDs listed in the [system overview](system_overview.md).
- **The Runtime never dials Sessions or Environments.** Environments dials each Runtime; the earlier
  mode where a Runtime dialed the Gateway was removed deliberately.
