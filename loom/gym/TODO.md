# loom/gym TODO

- **Archive-backed eval reliability — tune CDX after the completed limiter
  rerun.** The 33-task `glm-4.5` archive-backed rerun with
  `--message-limit 1000` and `--compaction-threshold-tokens 115000` completed
  with 33/33 scored answers; the run notes and filler fetch counters are in
  <../wayback/cache/PLAN.md>. Remaining work: manually reprobe the known bad
  CDX query, burst-test cold CDX/replay misses, and confirm `in_flight` returns
  to zero while
  `wayback_cache_acquisition_failures_total{endpoint,reason,status}` separates
  queue timeout from upstream retry/backoff. Archive-service design/status is in
  <../wayback/cache/PLAN.md>, the eval-run procedure is in <k8s/README.md>,
  and IA API notes are in <../docs/archive_org_apis.md>.

- **Rename `baseline_llm.py`.** Once the bare one-shot LLM scaffold is gone, the
  module is purely the shared answer-schema + parse library (`question_schema`,
  `answer_instruction`, `parse_answer`, the per-question input models) — the name
  `baseline_llm` no longer describes it. Rename to something like `answer_schema.py`
  and update importers (`inspect_harness.py`, tests, BUILD).

- **The archive-only clamp is bypassable via the shared `docker-ci` daemon.** The
  eval's integrity rests on the per-sandbox network clamp: the contestant container
  sits on an `internal` compose network whose only route out is the wayback MITM
  proxy (pinned to `WAYBACK_AS_OF`), so it physically cannot read post-`as_of` or
  un-archived internet. That guarantee holds only relative to _its own_ compose
  project. When the eval runs against the shared `docker-ci` DinD (see
  <../../cluster/k8s/docker-ci/README.md>), anything else with the mTLS client key —
  or a contestant that escapes its container onto the daemon's host network / the
  shared `egress` bridge — can start an unclamped container with full egress,
  bypassing the proxy entirely. It would be nice to close this with a **dedicated
  "docker-for-agents" daemon**, separate from the general `docker-ci` one: the latter
  also serves ordinary CI Docker tests that legitimately need broad egress (registry
  / testcontainer pulls), so its internet can't simply be cut. The agents daemon, by
  contrast, can be locked down hard — a `CiliumNetworkPolicy` on the daemon pod that
  permits egress only to the in-cluster wayback-cache ClusterIP (plus cluster DNS and
  the image registry), so even a container that escapes the per-sandbox compose
  network still can't reach the live internet. The per-sandbox MITM clamp then becomes
  defense-in-depth rather than the sole barrier. A per-run throwaway daemon (torn down
  after each eval) is an alternative that also bounds blast radius.
