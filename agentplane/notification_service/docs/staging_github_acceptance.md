# Staging GitHub acceptance — 2026-10-04

App **5188971** is enabled; the operator confirmed installation on all their repositories.
[Rollout proof](https://github.com/agentydragon/ducktape/issues/8956#issuecomment-5983967691): 2/2 ready replicas,
successful migrations, Flux healthy; no database reset.

Verified on `agentydragon/ducktape`, without integration-app API calls:

- 13 real branch CI events (`workflow_run`, `check_run`, `check_suite`, `status`) reached inbox and harness.
- [A PR comment](https://github.com/agentydragon/ducktape/pull/8982#issuecomment-5984066218) produced one inbox entry
  matching both overlapping subscriptions.
- Reads preserved acknowledgement; the agent explicitly acknowledged through cursor 24 and cancelled test subscriptions.

Not proved: webhook-redelivery deduplication, restart recovery, revoked access, broader repository/event/fork coverage
or negative ingress probes. See [remaining work](../../plans/notifications.md). Debounce
[#8988](https://github.com/agentydragon/ducktape/pull/8988) is not part of this deployed proof.
