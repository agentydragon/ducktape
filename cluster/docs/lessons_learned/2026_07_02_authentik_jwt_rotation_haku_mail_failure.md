# 2026-07-02 — authentik-jwt-rotation failing hourly since the haku-mail rotation landed

## Symptom

`authentik-jwt-rotation` (CronJob, `agents-infra`) failed every hourly run from
~07:15Z onward. `kubectl get pods` showed the pod mounting all its secrets and
starting the container fine, then crashing and restarting under
`restartPolicy: OnFailure` until `backoffLimit: 2` was hit, at which point the
Job controller deleted the pod (the documented k8s footgun for `OnFailure`
Jobs — see the [upstream note][job-onfailure]).

[job-onfailure]: https://kubernetes.io/docs/concepts/workloads/controllers/job/#pod-backoff-failure-policy

## Root cause #1 (fixed, #2760): one bad rotation blocks the whole batch

`rotate.py`'s `main()` ran every configured rotation inside a single list
comprehension:

```python
rotated = [r.name for r in config.rotations if rotate_one(client, r, config)]
```

`rotate_one()` raises on any credential/audience/issuer mismatch. An exception
from _any_ rotation aborted the entire comprehension — so nothing after the
failing entry ever ran, and (worse) any rotation that succeeded _before_ the
failing one was never committed, since `commit_and_push()` only runs after the
full comprehension completes.

The `haku-mail` rotation (JWT for Haku's Stalwart mailbox, provider
`stalwart-haku`, #2724) was added to `rotations.yaml` shortly after the
07:15Z success. Every other configured rotation has 20+ days of remaining
token validity (`rotate_below_hours: 24`), so `haku-mail` is the only entry
that attempts a real mint each cycle — making it the sole point of failure
that took the whole job down, every hour, indefinitely.

Fixed in #2760 (landed on `devel` independently, same day, by a parallel
session working the same incident): per-entry try/except in the main loop,
logging each failure and continuing, committing whatever succeeded, still
exiting non-zero (Job stays `Failed`, alerting stays intact) if anything
failed.

## Root cause #2 (still open): why does haku-mail's own mint fail?

Not yet confirmed. Everything checked so far shows no config mismatch:

- The `stalwart_haku` Authentik provider/application Terraform resources
  (`tf/gitops/agent-machine-access/main.tf`) are byte-for-byte structurally
  identical to the already-working `grocy_mcp_haku_sf` provider (same SA,
  same `user_password` credential mode, same `implicit_consent` authorization
  flow, same property mappings, same 30d validity, same
  `client_type = "confidential"`).
- `haku-mail` and `haku-grocy` share the exact same underlying `haku`
  service-account app-password (`authentik_token.haku_grocy`), so credential
  value can't differ.
- The `stalwart-haku` OIDC discovery endpoint
  (`https://auth.allegedly.works/application/o/stalwart-haku/.well-known/openid-configuration`)
  resolves correctly and matches `grocy-mcp-haku-sf`'s shape exactly.
- The `haku-mail-client-credentials` k8s Secret exists and mounts cleanly
  (confirmed via `kubectl` events — no `FailedMount`), which also proves the
  Terraform apply for the Authentik provider succeeded (the k8s Secret's
  `client_id` is computed from the provider resource, so Terraform couldn't
  have created one without the other).
- `Push authentik-jwt-rotation` in CI succeeded on the relevant commit, so
  the deployed image wasn't stale/broken.
- #2760 independently ruled out: tofu drift, credentials-secret shape, and
  the token endpoint's `invalid_grant` behavior matching the working
  provider exactly (both reject bogus creds identically) — plus confirmed
  the missing seed SOPS file (`secrets/haku-mail-jwt.yaml` never committed)
  is the expected "first-ever mint" case, not a bug.

## Blocker (fixed, this change): no RBAC to read the pod's logs

`agents-infra` had **no `agent-rbac/` directory at all** — every agent
identity (Claude web, Haku, agent-box Codex) only had
`cluster-diagnostics-reader`, which deliberately excludes `pods/log` (see
`agent-rbac-base/README.md`). `kubectl auth can-i get pods/log -n
agents-infra` misleadingly reports `yes` (it only checks whether _any_ rule
matches the resource type, not the specific object), but actual
`kubectl logs` calls 403 unconditionally. `kubectl exec`/`port-forward` are
also unavailable (RBAC-denied, separately from the historical
`kubeapi-proxy` WebSocket-upgrade issue in
<2026_06_18_kubectl_exec_websocket_kubeapi_proxy.md>, which is already
fixed). Loki is unreachable from the `claude-sandbox` pod network (egress
NetworkPolicy blocks it).

Added `cluster/k8s/agents/authentik-jwt-rotation/agent-rbac/`
(`logs-configmaps-reader` RoleBinding for `agents-infra`), mirroring the
`kube-system`/`monitoring` agent-rbac pattern exactly. Once this merges to
`devel` and Flux reconciles, an agent can `kubectl logs` the next failing
`authentik-jwt-rotation` (or `attic-jwt-rotation`, which shares the
namespace) pod directly instead of racing the ~30s window before the Job
controller deletes it.

## Next step

Once this RBAC fix lands **and** #2760's fixed image is built and rolled out
by Flux image automation: catch the pod on the next hourly run
(`kubectl logs -n agents-infra <pod>` and/or `--previous`, ideally within a
few seconds of the `SuccessfulCreate` event — `backoffLimit: 2` gives a very
short window) and read the actual traceback for `haku-mail`'s mint failure.
