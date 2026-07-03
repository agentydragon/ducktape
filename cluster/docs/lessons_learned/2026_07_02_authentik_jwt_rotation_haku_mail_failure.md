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

## Blocker (fixed, #2766): no RBAC to read the pod's logs

`agents-infra` had **no `agent-rbac/` directory at all** — every agent
identity (Claude web, Haku, agent-box Codex) only had
`cluster-diagnostics-reader`, which deliberately excludes `pods/log` (see
`agent-rbac-base/README.md`). `kubectl auth can-i get pods/log -n
agents-infra` misleadingly reports `yes` (it only checks whether _any_ rule
matches the resource type, not the specific object), but actual
`kubectl logs` calls 403'd unconditionally. `kubectl exec`/`port-forward`
were also unavailable (RBAC-denied, separately from the historical
`kubeapi-proxy` WebSocket-upgrade issue in
<2026_06_18_kubectl_exec_websocket_kubeapi_proxy.md>, which is already
fixed). Loki was unreachable from the `claude-sandbox` pod network (egress
NetworkPolicy blocks it).

Fixed by adding `cluster/k8s/agents/authentik-jwt-rotation/agent-rbac/`
(`logs-configmaps-reader` RoleBinding for `agents-infra`), mirroring the
`kube-system`/`monitoring` agent-rbac pattern exactly. Once live, `kubectl
logs -n agents-infra <pod>` on the next failing run finally surfaced the real
traceback below.

**Removed again once root cause #2 was confirmed fixed and the CronJob
verified green** (operator judgment call: standing `pods/log` + `configmaps`
read access to a credential-handling namespace is broader than this specific
incident warranted once the actual bug was found). If `agents-infra` needs
agent-readable logs again for a future incident, re-add the same
`agent-rbac/` directory — the pattern above still applies.

## Root cause #2 (fixed, this change): `.sops.yaml` has no creation rule for `secrets/haku-mail-jwt.yaml`

The actual traceback, captured live from the pod once #2766's RBAC landed:

```text
haku-mail: rotating (remaining=none)
HTTP Request: POST https://auth.allegedly.works/application/o/token/ "HTTP/1.1 200 OK"
error loading config: no matching creation rules found
haku-mail: rotation failed; continuing with remaining entries
Traceback (most recent call last):
  File ".../rotate.py", line 363, in main
    if rotate_one(client, rotation, config):
  File ".../rotate.py", line 267, in rotate_one
    subprocess.run(["sops", "encrypt", "--in-place", str(rotation.sops_file)], check=True)
subprocess.CalledProcessError: Command '['sops', 'encrypt', '--in-place', 'secrets/haku-mail-jwt.yaml']' returned non-zero exit status 1.
```

The mint itself always succeeded (`200 OK` from Authentik — confirming
everything checked in root cause #1's investigation was in fact fine: the
`stalwart_haku` provider, the credentials secret, the SA, all correctly
configured). The failure was purely local: `secrets/haku-mail-jwt.yaml` was a
**brand new file** (`haku-mail` is the only rotation that's never minted
before), and `.sops.yaml` had a dedicated `path_regex` creation rule for
every _other_ rotation's output file (`claude-web-k8s-jwt.yaml`,
`haku-k8s-jwt.yaml`, `agent-box-codex-k8s-jwt.yaml`, `haku-grocy-jwt.yaml`,
`alloy-otlp-bearer-token.yaml`, ...) but **no rule was ever added for
`secrets/haku-mail-jwt.yaml`** when the `haku-mail` entry landed in
`rotations.yaml` (#2724). With no matching creation rule, `sops encrypt`
refuses to run at all — reproduced locally with the exact same command
(`sops encrypt --in-place secrets/haku-mail-jwt.yaml`), which fails
identically outside the cluster.

Fixed by adding the missing rule to `.sops.yaml`, mirroring
`secrets/haku-grocy-jwt.yaml`'s recipients exactly (admin + `haku` + all
user keys for break-glass — `haku` is the only agent identity that needs to
read this token; other agents don't).

## Takeaway

Adding a new `authentik-jwt-rotation` entry whose `sops_file` doesn't exist
yet requires a matching `.sops.yaml` creation rule in the _same_ change —
there's no generic catch-all for `secrets/*.yaml`, only per-file rules. This
is easy to miss because everything else about the rotation (Terraform,
credentials, RBAC) can be entirely correct and the failure only surfaces at
mint time, in a pod whose logs are hard to reach and whose crash message
(`no matching creation rules found`) doesn't obviously point at the
`rotations.yaml` diff that caused it. Consider a `cluster/validation` check
that every rotation's `sops_file` (and `k8s_secret.path`) matches some
`.sops.yaml` creation rule, so this class of bug fails CI instead of the
CronJob.
