# loom/gym TODO

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
  bypassing the proxy entirely. It would be nice to close this: give the eval its
  own throwaway DinD (per-run, torn down after) rather than a shared persistent one,
  or enforce daemon-side egress restrictions (no container may reach anything but the
  in-cluster cache), so the archive clamp can't be sidestepped through the daemon.
