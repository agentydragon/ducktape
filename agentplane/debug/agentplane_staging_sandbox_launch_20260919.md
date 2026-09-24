# Dogfooding the agentplane-staging sandbox as a Haku launch method (2026-09-19)

On 2026-09-19 a claude.ai session provisioned and used a sandbox purely through the Action
Service's `sandbox` ActionGroup (the `mcp__Agentplane_staging__*` tools), as the `claude-ai`
ServiceAccount in `agentplane-staging`, in place of haku-console's `sandbox` MCP server
(`haku/sandbox/`), and exercised what `ducktape` and `haku-state` document as available to Haku.
The findings are evidence for whether and how this path should become a Haku launch method.

## Verdict

The Action round trip (`list_actions` → `get_action_policy` → `request_action` →
`get_action_request`/`list_action_request_events`) and the sandbox lifecycle it carries work as
documented, and credential substitution makes the Kubernetes API and Forgejo usable from the box
with nothing to set up inside it. It is not yet a substitute for Haku's own perimeter
(`oidc-ksbx-groups:haku` plus haku-console's `haku_v1` profile): `claude-ai` has no writable
namespace, reads one Secret, and lacks part of Haku's reach (finding 4), its one exec environment lacks
Haku's tools (3), and its sandboxes hold a small shared quota until someone disposes them (1, 2).

## What works end-to-end (verified live)

- **Auto-approved lifecycle.** `ActionPolicyBinding` `claude-ai-reads` grants `claude-ai` the
  `sandbox-self` set, so all five sandbox Actions ran without a human decision, 100–300 ms each
  from submit to result. A clean `create` reached `Ready=True` in ~45 s.
- **Kubernetes as the box's own identity.** Plain `curl` with
  `Authorization: Bearer agentplane-credential-kubernetes-workload` returned the
  `SelfSubjectReview` recorded in `agentplane/docs/sandbox_actions.md`: the preset `HTTPS_PROXY`
  and the interception CA mounted over `/etc/ssl/certs/ca-certificates.crt` leave nothing to
  configure.
- **Forgejo as `haku`, git and REST.**
  `git clone http://haku:agentplane-credential-forgejo-haku@forgejo-http.forgejo.svc.cluster.local:3000/haku/haku-state.git`
  cloned `haku-state` with no `~/.netrc` or Secret read, and `GET /api/v1/user` through the same
  substitution returned `haku`. Push was not tried; the credential is the account's own password
  with no method limit (`cluster/cdk8s/agentplane/egress_staging_credentials.py`), so it carries
  write.
- **Unlisted hosts fail closed.** `github.com` is refused with 403 (`claude-ai` has no
  `github-public` binding), and so is Forgejo's short name `forgejo-http.forgejo:3000`: a rule
  admits only the host name it lists, the gotcha `sandbox_actions.md` records for
  `kubernetes.default.svc`.

## Open findings

### 1. Forgotten sandboxes hold the shared CPU quota indefinitely

The first `create` failed at once:

```text
Ready=False reason=ReconcilerError message=Error seen: pods "claude-ai-haku-probe" is forbidden:
exceeded quota: agentplane-staging-quota, requested: limits.cpu=2500m, used: limits.cpu=10500m,
limited: limits.cpu=12
```

Two `claude-ai` sandboxes left idle for 13–14 hours held 5 of the 12 cores, and only disposing
them freed the quota. Nothing expires these boxes: `agentplane/sandbox_actions/inventory.py`
stamps every Sandbox `shutdownPolicy: Retain` with no `shutdownTime` (its `TODO(sandbox-lifetime)`
records the fix below), the integration app's
Sandboxes carry no expiry either (`agentplane/app/inventory.py`), and nothing in
`agentplane-staging` sweeps them. haku-console's sandbox claims carry `shutdownPolicy: Delete` and
a `shutdownTime` each `exec` pushes forward (`haku/sandbox/kubernetes_client.py`; 8 h, then at
least 2 h past each exec).

All of them share `agentplane-staging-quota` (`cluster/cdk8s/agentplane/rbac.py`), which is
tighter than its comment's "roughly four concurrent sandboxes". A sandbox costs 2500m of
`limits.cpu` (2 cores for `runner`, plus the `LimitRange`'s 500m default for `egress-sidecar`,
which sets no CPU limit), but the namespace's eleven service Pods take 500m each, 5.5 of the 12
cores (observed 2026-09-23). Two sandboxes fit at once, the app's included, and one forgotten box
halves that.

**Recommendation:** bound these sandboxes' lifetime as haku-console does, with the Sandbox's own
`shutdownTime` and `shutdownPolicy: Delete` pushed forward by `exec`, or sweep long-idle
`sandbox-actions.agentplane.allegedly.works/managed=true` Sandboxes; and size the quota for the
services it also holds. Meanwhile `agentplane/docs/sandbox_actions.md` should tell a caller that
an exceeded-quota `ReconcilerError` is a cue to `list` and dispose its own stale boxes.

### 2. A quota-blocked sandbox waits out the controller's error backoff

After the leaked boxes were disposed, `info` on the still-blocked `haku-probe` returned the same
condition and `lastTransitionTime`, and no Pod appeared, for as long as it was polled; `dispose` +
`create` then reached `Ready` in ~45 s. The controller does retry, but not when quota frees:
agent-sandbox v0.5.5 returns the Pod-create error from `Reconcile`, so controller-runtime requeues
the Sandbox on per-object exponential backoff (5 ms doubling to a 1000 s cap), and it watches only
Sandboxes and their own Pods and Services, never `ResourceQuota`. Each wait roughly equals the time
the box has already spent failing, up to ~17 minutes. This is read from upstream
`controllers/sandbox_controller.go` and controller-runtime v0.24.1; the late retry itself was not
observed.

**Recommendation:** `sandbox_actions.md` now names the exceeded-quota `ReconcilerError` and says
`create` does not wait, but "queued behind a quota" reads as if the box starts once quota frees.
Say instead that it clears only at the controller's next backoff retry, up to ~17 minutes after
the quota frees, while `dispose` + `create` reconciles at once.

### 3. The only environment is the runner image, and its description promises Python

Staging's `sandbox` group offers one environment, `runner` (`cluster/cdk8s/agentplane/staging.py`):
the integration app's `agentplane-runner` template, which the comment there already calls the wrong
destination. The description `create` renders for it, "python, git and the agent harnesses",
overstates what a command gets: `command -v` in the box found no `python3`, `pip3`, `kubectl`,
`tea`, `jq`, `gh`, `bazel`/`bazelisk` or `openssl`, and uid 1000 has no `/etc/passwd` entry. The
image's Debian packages are `curl`, `git` and `ripgrep` (`trixie_agentplane_runner` in
`MODULE.bazel`), and its only Python is the runner's own hermetic interpreter, which `exec`'s
`bash -lc` does not put on `PATH`. haku-console's box
(`cluster/k8s/haku/workspaces/image/Dockerfile`) bakes what `haku-state`'s tooling calls:
`python3`, `kubectl`, `tea`, `jq`, `gh`, `ruff`, bazelisk with a JDK, and `build-essential`.

A Nix sandbox image now builds and publishes (`agentplane/sandbox_image/default.nix`, pushed by
`.github/workflows/agentplane-sandbox-image.yml`) with a full `python3`, `kubectl`, `jq`,
`openssl` and a `runner` passwd entry, but no `SandboxTemplate` uses it, so `runner` is still the
only environment (as of 2026-09-24). It still lacks `tea`, `gh`, `ruff`, bazelisk and a compiler.

**Recommendation:** make the `runner` description say what a command can use (`git`, `curl`,
`ripgrep`; no Python), and offer the sandbox image as the default environment, through its own
`SandboxTemplate` with the egress sidecar, CA and proxy environment.

### 4. `claude-ai`'s reach still falls short of Haku's

Haku's perimeter, as `cluster/k8s/agents/agent-rbac-base/README.md` and `haku-state`'s
`memory/credentials.md` describe it: full CRUD in `haku-sandbox` and the Secrets there (Plaid
Postgres, ActivityWatch, the haku mailbox JWT, the haku-console MCP token), cluster-wide
diagnostics, metadata and logs in agent-readable namespaces, and what haku-console still fronts
(its `sandbox` and `grants`) under `haku_v1`. What `claude-ai` lacks of it on `devel`, as of
2026-09-24:

- **Kubernetes:** it holds `cluster-diagnostics-reader`, the metadata and log readers of the
  `agent-readable-*` namespaces, and `get` on the one Secret
  `agentplane-staging/coinbase-api-credentials` (`agent-rbac-base/README.md` section 6). No role
  in `haku-sandbox`, so no Plaid query Pod and none of its Secrets.
- **Egress:** Forgejo as `haku`, the Kubernetes API, the package mirrors, Google's read APIs
  (`google-readonly`), Grocy SF reads (`grocy-sf-readonly`), Home Assistant's state and history
  reads (`home-assistant-readonly`) and Coinbase are bound. GitHub is not (`TODO(github-egress)`
  in `cluster/cdk8s/agentplane/actions_staging_policies.py`), and nothing reaches Plaid,
  ActivityWatch, the mailbox or haku-console.
- **Actions:** `cluster/cdk8s/agentplane/staging.py` configures a group for every server that left
  haku-console (`kubectl_passthrough_mcp` as `kubernetes_admin`); `grants` has no Action Service
  counterpart. `claude-ai-reads` auto-approves the GitHub, Home Assistant, Gmail, Calendar, Tana
  and Grocy SF reads and the sandbox set; every write waits for a human. Live on 2026-09-24,
  `grocy_sf`, `gmail` and `google_calendar` list their Actions as available.

**Recommendation:** the scope is now written down, in `haku/runtime/claude_web_env/run.md` and
`haku-state`'s `sources/` guides, and a run sweeps agentplane requests as it did console tool calls
(`haku-state` `memory/procedures/tool_calls.md`). What is left is deliberate: Plaid, ActivityWatch
and the mailbox have no route, `grants` waits on the Action Service gaining expiring grants, and
the exec environment is finding 3.
