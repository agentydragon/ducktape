# Dogfooding the agentplane-staging sandbox as a Haku launch method (2026-09-19)

Task: stop using haku-console's `sandbox` MCP server (`haku/sandbox/`) and instead provision and
use a sandbox purely through the new `mcp__Agentplane_staging__*` Action Service tools, as `claude-ai`
in `agentplane-staging`, then exercise what `ducktape` and `haku-state` document as available. This
records what worked, what didn't, and why — as evidence for deciding whether/how this method should
mature into a real Haku launch path. No cluster config was changed; only two abandoned `claude-ai`
sandboxes and my own probe sandbox were disposed through the tool under test.

## Verdict

The Action Service round trip (`list_actions` → `get_action_policy` → `request_action` →
`get_action_request`/`list_action_request_events`) and the sandbox lifecycle it carries
(`create`/`info`/`exec`/`list`/`dispose`) work exactly as documented, and egress substitution for
Kubernetes and Forgejo is genuinely transparent — no credential setup needed inside the box at all.
But this is **not yet a like-for-like substitute** for the haku-console sandbox / `oidc-ksbx-groups:haku`
perimeter that `haku-state`'s own manual documents: the caller identity, RBAC, credentials and exec
image are all substantially narrower today. Treat the two as different tools with different reach
until that gap is closed or the manual is corrected to say so.

## What works end-to-end (verified live against `agentplane-staging`)

- **Auto-approval**: `claude-ai` is bound to `sandbox-self` (`ActionPolicyBinding
claude-ai-github-reads`), so all five sandbox Actions execute with no human decision, typically
  in 100-300ms end to end (submit → policy → dispatch → result).
- **Lifecycle**: `create` → poll `info` for `Ready` reproduced every condition
  `x/agentplane/docs/sandbox_actions.md` names (`ReconcilerError`, `DependenciesNotReady` with "Pod
  exists with phase: Pending", `DependenciesReady` with "Pod is Ready"). Time from a clean `create`
  to `Ready=True` was ~45s.
- **Kubernetes egress**: `curl -H 'Authorization: Bearer agentplane-credential-kubernetes-workload'
https://kubernetes.default.svc.cluster.local/apis/authentication.k8s.io/v1/selfsubjectreviews`
  (POST) returns `system:serviceaccount:agentplane-staging:claude-ai` with matching
  `pod-name`/`pod-uid` extras — reproducing the exact verification already recorded in
  `sandbox_actions.md`. No `kubectl` needed; plain `curl` through the pre-set `HTTPS_PROXY` works
  because the intercepting proxy's CA is already in `/etc/ssl/certs/ca-certificates.crt`.
- **Forgejo egress, both surfaces**: `git clone http://haku:agentplane-credential-forgejo-haku@
forgejo-http.forgejo.svc.cluster.local:3000/haku/haku-state.git` cloned real `haku-state`
  (`AGENTS.md`, `SOUL.md`, `MEMORY.md`, `TODO.md`, ...) with **zero credential setup** — no
  `~/.netrc`, no secret read, just the FQDN and a placeholder password. The REST API
  (`GET /api/v1/user`) through the same substitution independently confirmed the identity as
  `haku`/`haku@allegedly.works`. This is a genuine UX improvement over the old flow's
  `~/.netrc`/Secret-read bootstrap.
- **Egress boundaries are exactly as narrow as documented**: `github.com` — 403 (`claude-ai` has no
  `github-public` `EgressBinding`, exactly as flagged in
  `cluster/cdk8s/agentplane/actions_staging_policies.py`'s `TODO(github-egress)`); the Forgejo
  **short-form** hostname `forgejo-http.forgejo:3000` (no `.svc.cluster.local`) — 403, empirically
  confirming the "host admitted by name, not address" gotcha extends to abbreviated DNS forms, not
  just IPs; `pypi.org` — 200 (packages policy).
- **Caller isolation**: `sandbox.list` returned only my own sandbox; a pre-existing, unrelated
  `test-ydxdf` Sandbox (the integration app's, not sandbox_actions') never appeared and wasn't
  touched.
- **Recovery**: disposing and recreating a stuck sandbox is a reliable, tool-native fix (see below).

## Friction found

### 1. Namespace CPU quota is trivially exhausted by forgotten sandboxes, which never expire

My first `create` failed immediately:

```text
Ready=False reason=ReconcilerError message=Error seen: pods "claude-ai-haku-probe" is forbidden:
exceeded quota: agentplane-staging-quota, requested: limits.cpu=2500m, used: limits.cpu=10500m,
limited: limits.cpu=12
```

`kubectl -n agentplane-staging get sandboxes.agents.x-k8s.io` showed why: two `claude-ai-owned`
sandboxes (`claude-ai-conditions-check`, `claude-ai-egress-check`) had been sitting idle for 13-14
hours, each costing `limits.cpu=2500m` (2 cores from the runner container + a 500m default the
namespace `LimitRange` applies to the sidecar, which sets no CPU limit of its own) — 5 of the
namespace's 12-core hard cap, alongside a same-day `agentplane-egress` rollout that transiently
doubled its own pod count. I disposed both through `sandbox.dispose` (they were mine to clean up)
and quota returned to `5.5`/`12` cores.

This is a structural gap, not a one-off: `x/agentplane/sandbox_actions/inventory.py` stamps every
Sandbox with `shutdownPolicy: Retain` deliberately ("a box whose caller is still working in it must
not be collected out from under them on a schedule nobody set"), unlike the haku-console tool's
`initial_ttl_seconds`/`exec_ttl_extension_seconds` lease that auto-expires an abandoned claim. A
namespace-wide quota shared by every tenant plus per-caller sandboxes with **no expiry at all**
means one forgetful session permanently taxes (or, as here, fully blocks) everyone else's
`sandbox.create` until a human or another agent happens to notice and dispose it.

**Recommendation**: give sandbox_actions-created boxes a bounded idle TTL (mirroring the old tool),
or at least a periodic sweep of long-idle `sandbox-actions.agentplane.allegedly.works/managed=true`
Sandboxes; short of that, `x/agentplane/docs/sandbox_actions.md` should tell callers that a
`ReconcilerError` quota message is reason to check `sandbox.list` for their own stale boxes before
assuming the namespace itself is out of room.

### 2. A quota-blocked sandbox does not self-heal once quota frees up

After disposing the two leaked sandboxes, polling `sandbox.info` on the still-failed `haku-probe`
kept returning the **identical** stale condition and `lastTransitionTime`, confirmed also via direct
`kubectl get sandboxes.agents.x-k8s.io claude-ai-haku-probe` — no Pod had been (re-)attempted. The
Agent Sandbox controller apparently doesn't watch `ResourceQuota` and only re-reconciles on its own
resync interval or a spec change, neither of which a `sandbox.info` poll triggers. The only fix I
found was **dispose + recreate** (a fresh object triggers a fresh reconcile), which then succeeded
normally (`DependenciesNotReady` → `DependenciesReady` in ~45s).

**Recommendation**: state this explicitly in `sandbox_actions.md`: a `ReconcilerError` condition
that doesn't clear shortly after its stated blocker is resolved should be treated as stuck, and
`dispose`+`create` is the documented remedy — not more polling.

### 3. The only offered environment is the full agent-runner harness image, not a shell

Staging's `sandbox` group offers exactly one environment (`cluster/cdk8s/agentplane/actions.py`):

```python
"runner": {
    "template": "agentplane-runner",
    "container": "runner",
    "default_cwd": "/state",
    "description": "The shared runner image: python, git and the agent harnesses.",
}
```

The code comment right above it already flags this as provisional: _"its workload container is the
runner image, which is the wrong destination -- a box to run commands in wants neither the harnesses
nor the state volume."_ Empirically, the description also **overstates what's exec-accessible**:
`command -v` and a filesystem-wide `find` inside the box turned up only `git` and `curl` on `PATH`.
`python3`, `pip3`, `kubectl`, `tea`, `sops`, `nix`, `bazel`/`bazelisk`, `jq`, `gh`, and `openssl` are
all absent — no Python interpreter exists anywhere on the filesystem (`/usr/local/bin` holds only a
214MB standalone `claude` binary). There's also no `/etc/passwd` entry for uid 1000. This is
materially thinner than the haku-console sandbox image
(`cluster/k8s/haku/workspaces/image/Dockerfile` + `haku-sandbox-setup.sh`), which ships
kubectl/tea/sops/nix.

**Recommendation**: correct the Action's environment description to what's actually there
(git + curl; no Python), and — if this method is meant to carry real Haku work — add a second,
lighter `SandboxEnvironment` purpose-built for ad hoc exec (as the code comment already anticipates)
carrying the tools `haku-state/memory/procedures/run.md` actually expects, rather than reusing the
harness image.

### 4. This identity's reach is much narrower than what `haku-state` documents as "available"

`haku-state/memory/credentials.md` and `memory/procedures/run.md` describe Haku's perimeter as the
Kubernetes principal `oidc-ksbx-groups:haku`: full CRUD in `haku-sandbox`, cluster-wide read-only
diagnostics (nodes/pods/events/deployments/Flux/certs/metrics), infra-namespace pod-logs/configmaps,
and a table of Secrets (Plaid Postgres, Google Drive/Tasks, ActivityWatch, the haku mailbox JWT, the
haku-console MCP token for the tool-request/approval queue).

None of that is reachable from this new mechanism. `curl`'s `SelfSubjectRulesReview` against both
`agentplane-staging` and `haku-sandbox` came back **identical and empty** beyond the cluster-wide
baseline every authenticated principal gets (self-review endpoints, and unrelated KubeVirt/CDI list
rules that are ambient cluster defaults, not anything scoped to `claude-ai`). So today, from this
sandbox: no RBAC of any kind, no Plaid/Google/ActivityWatch/mailbox secret, and no path to the
haku-console MCP approval queue that `run.md` step 3 ("Sweep approved tool-call results") depends
on. What _does_ carry over cleanly: `haku-state` itself (read verified; write not separately tested,
to avoid polluting Haku's real memory repo with throwaway commits — it's the same whole-account
Forgejo credential either way), GitHub reads via the Action Service's own `github` group, package
mirrors, and a mechanically-correct-but-currently-empty Kubernetes identity.

**Recommendation**: this is a scope decision for whoever owns the migration, not something to guess
at from here — but it should be written down. Either (a) extend `claude-ai`'s RBAC/EgressBindings/
Secrets deliberately, action by action, until this path has real parity with `oidc-ksbx-groups:haku`
before treating it as Haku's new home, or (b) if the intent is narrower (e.g. a general
Action-based exec/GitHub-reads surface, not a Haku-run replacement), say so in `haku-state` — a
runtime-specific entrypoint note, the same pattern `haku/runtime/claude_web_env/run.md` already
uses for the web-home's own environment differences — so a future run under this launch method
doesn't spend time hunting for Plaid/mailbox/console access that was never wired here.

### 5. Minor tool ergonomics

- `request_action`'s `title` silently enforces a 60-character max (`string_too_long`) with no hint
  in the tool description until you hit it; worth a one-line callout since every single call has one.
- `create`'s `wait_seconds`/`wait_until=terminal` only waits for the **Action's own** terminal state
  (the object now exists), never the underlying Sandbox's `Ready` condition — clearly documented,
  but easy to reflexively expect a generous `wait_seconds` on `create` to hand back a usable box. A
  first-time caller (me) made exactly that assumption before re-reading `info`'s own docstring.

## What to keep

The credential-substitution design is the standout: nothing inside the box ever held a real secret,
yet `curl`/`git` worked immediately with no bootstrap step, and an unresolved or wrong-host
placeholder failed closed (403) rather than leaking anything. If sandbox_actions grows a lighter exec
environment and a lifecycle bound, the mechanism underneath it is already solid.
