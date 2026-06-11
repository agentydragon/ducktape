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

- **Automate sandbox-image staging into the in-cluster eval Job.** `loom/gym/k8s/`
  currently documents a manual step 2 (pull `wayback-proxy` from GHCR + `docker build`
  `loom-gym-sandbox` into the docker-ci daemon before running the Job). Fold this into
  an initContainer on `eval-job.yaml` (docker CLI + the mTLS secret), so a run is a
  single `kubectl apply`. The sandbox Dockerfile has no local context, so
  `docker build -t loom-gym-sandbox:latest -` from a ConfigMap-mounted Dockerfile works.
