# #4943 GitHub egress spike — operator runbook

Prove real GitHub traffic end to end through the colocated Console egress proxy
([#4942](https://github.com/agentydragon/ducktape/issues/4942) /
[#4965](https://github.com/agentydragon/ducktape/pull/4965)), exercised from the
**public-coder-agent OpenClaw pod**: standing-policy admission with `github-bot` credential
substitution (Bearer and git-over-HTTPS Basic), deny without an allowance, the temporary-grant
path (deny → `create_grant` → retry → release → deny), and fail-closed refusal. Success is the
gate for repointing the fleet Kyverno injection at this listener
(`cluster/k8s/haku/console/README.md` § Colocated egress proxy → Deferred). Report results on
[#4943](https://github.com/agentydragon/ducktape/issues/4943).

Everything here runs as the operator (cluster direnv kubeconfig). The spike sends requests from
the live agent pod but changes no state in it: every command below is per-invocation (no
`git config --global` — the pod's `HOME` is the agent's persistent PVC), and the only cluster
mutation is approving/releasing one temporary grant.

## Identity: who the fence sees

The sidecar presents one static fence credential (`HAKU_EGRESS_FENCE_CREDENTIAL`), so the decide
service resolves **every** fenced request to the **haku Agent**, whichever pod sent it. Driving
the spike from public-coder's pod therefore proves routing, admission, substitution, and the
grant lifecycle — not per-agent attribution: through the fence this pod authenticates to GitHub
as the haku bot, and a grant covering fenced traffic must belong to the haku Agent. Per-agent
fence identity (public-coder reaching GitHub as `agentydragon-agent` through the fence, with its
own credential substituted) is #4670's minted per-claim fence credential work. Until then the
iron proxy remains the agent's production GitHub path, untouched by this exercise.

## What the spike PR deployed

- **Fence wiring on the public-coder-agent OpenClaw pod**
  (`cluster/k8s/agents/public-coder-agent/app/deployment.yaml`): the fleet
  `inject-haku-egress-proxy` policy's own CA volume + mount (`/egress-proxy-ca`) — the exact
  wiring the policy's preconditions treat as "already fenced", so a later widening of the fleet
  injection to this namespace skips the pod instead of stamping port-8080 env over its iron
  values — plus the inert `HAKU_GITHUB_TOKEN=github-token-placeholder` the decide service
  substitutes. The pod's `HTTP_PROXY`/`HTTPS_PROXY` still point at the iron proxy: the fence is
  reachable (a new rule in `app/networkpolicy-egress.yaml` admits `haku-console:8888`), not the
  default route — each spike request opts in with `-x`.
- **`haku-egress-proxy-ca-cert` trust Bundle** extended to write into `public-coder-agent`
  (`cluster/k8s/agents/haku-egress-proxy/trust-bundle.yaml`), so the pod can verify the fence's
  interception leaves.
- **`egress_decide.standing_policies`** (`cluster/k8s/haku/console/config.yaml`):
  `haku-github-api` (api.github.com, API methods) and `haku-github-git` (github.com, GET+POST
  for smart HTTP), both redeeming `github-bot` — authored against the **haku** agent id, the
  identity fenced traffic actually presents (see above). `codeload.github.com` is deliberately
  _not_ standing — it is the temporary-grant leg's target.
- **`http_grants`** in-process MCP server, exposed to every access profile (operator ruling on
  #4986): any Agent may ask for egress. Deliberately in no auto-approval policy: `create_grant`
  must be manually approved (auto-approved calls cannot mint grants), so every call here queues
  for the operator. The identity constraint above still binds the spike: only a grant owned by
  the haku Agent matches fenced traffic today, so the grant leg below runs as haku.

## Pre-checks (read-only)

All must pass before entering the pod.

```bash
# Flux applied the spike commit (Console config + agent wiring + trust bundle, all Ready).
kubectl get kustomization -n ducktape-flux haku-console public-coder-agent-app haku-egress-proxy

# Console pod Ready with BOTH containers (server + egress-proxy sidecar), post-roll.
kubectl -n haku-console get pods -l app.kubernetes.io/name=haku-console
kubectl -n haku-console get deploy haku-console -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\n"}{end}'

# The fenced-workload listener Service exists (8888 -> sidecar).
kubectl -n haku-console get svc haku-egress-proxy

# Decide oracle wired and answering on loopback: the server logs no load_egress_decide error,
# and the sidecar starts its listener. (The oracle has no Service — acceptance criterion 14 —
# so "answering" is observed from the server/sidecar logs, not probed over the network.)
kubectl -n haku-console logs deploy/haku-console -c server --since=24h | grep -i -E 'egress|decide' | tail -20
kubectl -n haku-console logs deploy/haku-console -c egress-proxy --since=24h | tail -20

# The github-bot credential loaded: this grep MUST return nothing. A hit means the ESO secret
# had not synced at startup and the handle was skipped — substitution would silently not apply.
kubectl -n haku-console logs deploy/haku-console -c server --since=24h | grep "skipping egress credential"

# ESO secrets synced (github token + proxy/fence identity).
kubectl -n haku-console get externalsecret haku-egress-github-token haku-egress-proxy-identity
kubectl -n haku-console get secret haku-egress-github-token haku-egress-proxy-identity

# The fence trust bundle reached the agent's namespace, and the agent pod is Ready.
kubectl -n public-coder-agent get configmap haku-egress-proxy-ca-cert
kubectl -n public-coder-agent get pods -l app.kubernetes.io/name=public-coder-agent
```

In a second terminal, keep the sidecar's decision log streaming for the whole spike — it is
the primary evidence channel (`allow`/`deny` per admission, `decision_id`, and
`substitutions: N of M applied`; header values are never logged):

```bash
kubectl -n haku-console logs deploy/haku-console -c egress-proxy -f
```

## The spike workload: the running agent pod

No box is provisioned — the workload is public-coder's live pod:

```bash
POD=$(kubectl -n public-coder-agent get pod -l app.kubernetes.io/name=public-coder-agent -o jsonpath='{.items[0].metadata.name}')
```

**Verify the wiring landed, and only as an opt-in** — the pod carries the fence CA mount and the
placeholder, while its one `HTTP_PROXY` still names the iron proxy (the production path this
spike must not disturb):

```bash
kubectl -n public-coder-agent get pod "$POD" -o jsonpath='{.spec.containers[0].env}' | tr ',' '\n' | grep -c '"HTTP_PROXY"'   # expect 1
kubectl -n public-coder-agent get pod "$POD" -o jsonpath='{.spec.containers[0].env}' | grep -o 'public-coder-agent-proxy[^"]*' | head -1   # iron, not 8888
kubectl -n public-coder-agent exec "$POD" -c openclaw -- sh -c 'ls /egress-proxy-ca/ && echo "placeholder=$HAKU_GITHUB_TOKEN"'
```

Open a shell and set the per-request fence coordinates (nothing here is persisted; git gets the
interception CA per invocation with `-c`, because git links a TLS stack that reads neither
`SSL_CERT_FILE` nor `CURL_CA_BUNDLE`, and the pod's system bundle trusts iron, not the fence):

```bash
kubectl -n public-coder-agent exec -it "$POD" -c openclaw -- bash
FENCE=http://haku-egress-proxy.haku-console.svc.cluster.local:8888
FENCE_CA=/egress-proxy-ca/ca-certificates.crt
```

## Spike steps

Run inside the pod unless stated. `$HAKU_GITHUB_TOKEN` is the inert `github-token-placeholder`
from the Deployment — it authenticates nothing by itself, which is the point. (`$GH_PAT`, the
iron placeholder, means nothing to the fence; the two paths share no credential material.)

### 1. Standing allow, Bearer substitution

```bash
curl -sS -x "$FENCE" --cacert "$FENCE_CA" -H "Authorization: Bearer $HAKU_GITHUB_TOKEN" https://api.github.com/user | head -5
```

Expect the **haku bot account's** login in the response — not `agentydragon-agent`: the fence
resolves this pod's traffic to the haku Agent and substitutes the haku bot credential (the #4670
identity gap made visible). Self-evidencing: if substitution had not happened, GitHub would
answer `401 Bad credentials` for the raw placeholder. Sidecar log:
`allow GET api.github.com:443 -> <ip> decision_id=standing:haku-github-api (substitutions: 1 of 1 applied)`
(after the tunnel's `allow CONNECT api.github.com:443 ... decision_id=standing:haku-github-api`).

Unauthenticated reachability under the same entry (no placeholder → nothing to substitute,
`0 of N applied` is correct):

```bash
curl -sS -x "$FENCE" --cacert "$FENCE_CA" https://api.github.com/rate_limit | head -3
```

### 2. Basic substitution (the git-over-HTTPS form)

```bash
curl -sS -x "$FENCE" --cacert "$FENCE_CA" -u "x-access-token:$HAKU_GITHUB_TOKEN" https://api.github.com/user | head -5
```

Expect the bot login again. This exercises the proxy-side decode → swap → re-encode of the
base64 `Basic` payload (`haku/egress/addon.py _swap_placeholder`); a raw placeholder would be
`401`.

### 3. Real git over HTTPS

```bash
git -c http.proxy="$FENCE" -c http.sslCAInfo="$FENCE_CA" clone --filter=blob:none \
  https://github.com/agentydragon/ducktape.git /tmp/ducktape-spike
```

Expect a working clone; sidecar shows `standing:haku-github-git` allows for `github.com:443`
GET (`/info/refs`) and POST (`git-upload-pack`).

Git carrying the substituted Basic credential itself (public clones send no auth, so force a
401 challenge — GitHub answers 404 `Repository not found` to _valid_ credentials and keeps
answering 401 `Authentication failed` to bad ones):

```bash
git -c http.proxy="$FENCE" -c http.sslCAInfo="$FENCE_CA" ls-remote \
  "https://x-access-token:${HAKU_GITHUB_TOKEN}@github.com/agentydragon-agent/egress-spike-does-not-exist.git" 2>&1 | tail -2
```

Expect `Repository not found.` (authenticated, then 404) and a
`substitutions: 1 of 1 applied` line for the second `github.com` request in the sidecar log.
`Authentication failed` would mean the placeholder went upstream unsubstituted. Cleaner
alternative if the bot PAT can read any private repo: `git ls-remote` that repo and expect its
refs.

### 4. Denied origin (no standing policy, no grant)

```bash
curl -sS -x "$FENCE" --cacert "$FENCE_CA" https://example.com/ ; echo "exit=$?"
```

Expect curl error 56, `Received HTTP code 403 from proxy after CONNECT`; sidecar logs
`deny CONNECT example.com:443: no active HTTP grant covers the origin`. Nothing was forwarded.
(The same request through the pod's default iron path would succeed — this agent's iron egress
is a documented open waiver. The deny is the fence's allowlist-by-decision posture, which is
exactly what adoption trades that waiver for.)

The realistic in-scope variant — a GitHub tarball redirects api.github.com → codeload, and
codeload is outside standing scope:

```bash
curl -sSL -x "$FENCE" --cacert "$FENCE_CA" -o /tmp/dt.tgz https://api.github.com/repos/agentydragon/ducktape/tarball/devel ; echo "exit=$?"
```

Expect the api request allowed, then the redirect's `CONNECT codeload.github.com:443` denied
(curl error 56). This deny's server-side decision carries the canonical `grant_scope`
`{scheme: https, host: codeload.github.com, port: 443}` — exactly what to grant next.

### 5. Temporary grant: create → approve → retry → release → deny

From a session authenticated as the **haku Agent** (Haku's console chat, or a Claude session
wired to the console MCP) — not public-coder's: grants bind the calling Agent, and fenced
traffic presents the haku fence credential, so only a haku-owned grant can match (the same
#4670 identity constraint again). Submit — by generated proxy tool or by name:

```json
call_mcp_tool("http_grants", "create_grant", {
  "grants": [
    {
      "origin": { "scheme": "https", "host": "codeload.github.com", "port": 443 },
      "coverage": { "methods": ["GET", "HEAD"] }
    }
  ],
  "duration_seconds": 3600,
  "applies_to": "agent"
})
```

Pure reachability (no `credential_handle`) — the tarball redirect needs no credential at
codeload. `applies_to: agent` is required: the pod presents the static shared fence
credential, which has no session identity, so a session-scoped grant would never match.

- **Approve** the queued `create_grant` in the console approvals UI (it must queue — if it
  auto-approved, that is a policy regression and grant creation would refuse the provenance).
  Record the returned `grant_id`.
- **Retry** step 4's tarball fetch in the pod → expect success (`file /tmp/dt.tgz` → gzip).
  Sidecar: `allow CONNECT codeload.github.com:443 ... decision_id=grant:<grant_id>`.
- **Release**: `call_mcp_tool("http_grants", "release_grants", {"grant_ids": ["<grant_id>"], "reason": "spike complete"})`
  (operator-approve it), or let it expire.
- **Retry again** → back to the CONNECT 403 deny. Release/expiry denies the next admission —
  there is no proxy-local decision cache.

### 6. Fail-closed refusal

A decision-path error refuses rather than forwards — observable without touching the Console:

```bash
curl -sS -x "$FENCE" --cacert "$FENCE_CA" https://egress-spike-probe.invalid/ ; echo "exit=$?"
```

Expect curl error 56 with `502` from the proxy (`egress decision unavailable; refusing (fail
closed)`): resolution failed inside the gate, and the pre-set refusal stood. Sidecar logs
`egress decision failed (gaierror) ...; refusing`. (A stronger, disruptive variant — scaling
the Console down and watching every request 502 — is the operator's call; it severs live
Console sessions and is not required for #4943.)

## Evidence to capture

- Terminal transcripts of steps 1–6 (the placeholder is committable; never paste real token
  values — none should ever appear in the pod, which is itself part of the claim).
- The sidecar log segment covering the run: per-step `allow`/`deny` lines with
  `decision_id=standing:haku-github-{api,git}` / `decision_id=grant:<id>` provenance and
  `substitutions: N of M applied` counts; the `server` container's matching
  `egress decision ...` lines if more detail is wanted.
- `kubectl -n haku-console logs deploy/haku-console -c egress-proxy --since=6h | grep -c github-token-placeholder`
  → `0`: header values (even inert ones) never enter proxy logs.
- The single-iron-`HTTP_PROXY` pod-env check from the workload section (the production route
  never moved).
- The `create_grant`/`release_grants` tool-call IDs and the grant UUID with terminal status
  `released` (console audit ledger `/_console/tool-calls`, or `http_grants.get_grant`).

## Rollback / cleanup

```bash
rm -rf /tmp/ducktape-spike /tmp/dt.tgz   # inside the pod; /tmp is an emptyDir anyway
```

Release any still-active spike grants (step 5). Nothing else changed: the pod's fence wiring,
the standing policy, and the `http_grants` exposure are GitOps-managed and **stay** — they are
the first adoption instance, not scaffolding — and the agent's production egress ran through
iron untouched for the whole exercise. Retiring iron for this agent (repointing its default
`HTTP_PROXY` at the fence and moving its credential substitutions server-side) is gated on
#4670's per-agent fence identity, at which point this runbook's job is done and it is deleted
with the fleet repoint.
