# Plan: remote command execution on operator machines (Haku)

**Status:** design proposal, not built (operator, 2026-07-17).
**Ask:** let Haku run shell commands on the operator's machines (`rugged`, `wyrm2`,
`atlas`, `iguana`, …) with **no auto-approval**, and the ability to run as `agentydragon`
**or `root`** once the operator approves. Which transport — SSH over Nebula, a privileged
pod, something else?

## TL;DR recommendation

Build a new remote MCP server `hostexec-mcp` modeled on
<../../cluster/k8s/agents/kubectl-passthrough-mcp/>, registered in the console catalog so
it is approval-gated by construction. **Transport: SSH over the existing Nebula mesh** —
it is the only option that covers every target uniformly (including `atlas`, which is not
a k8s node) and reuses machinery that already exists (sshd + declarative `authorized_keys`
on every host). **Do not** ship it with a standing root SSH key as the whole authorization
story; make the SSH key a dumb transport and put the authorization in a **console-signed,
single-use, short-TTL capability token** that a small verifier on each host checks before
exec — the SSH analog of kubectl-passthrough's "the approval _is_ the credential." A
privileged pod-on-node is rejected as the primary transport (below).

## What is already decided for us

This is not a green field. The repo already solved the isomorphic problem — "let a
prompt-injectable agent run arbitrary privileged operations on operator infrastructure,
safely" — for `kubectl`, and the doctrine in <../docs/security.md> is explicit about the
shape any answer must take:

- **The container/perimeter is the trust boundary, never in-agent rules.** Haku runs
  `--dangerously-skip-permissions`; what limits it is the perimeter. So the exec capability
  cannot rely on Haku "behaving."
- **Execution runs under the approving operator's own authority, never a standing Haku
  credential.** kubectl-passthrough forwards the operator's own OAuth JWT straight to
  kube-apiserver (`cluster_auth_mode = "passthrough"`,
  <../../cluster/k8s/agents/kubectl-passthrough-mcp/app/configmap.yaml>); it holds no
  credential of its own. "There is no narrower RBAC backstop underneath either path" — the
  approval click in trusted console chrome is the gate.
- **Everything is in the ledger.** Every agent-originated call is a `McpToolCall` row scoped
  to its exact durable principal (`haku/console/tool_call_service.py`).
- **The approval surface is trusted chrome only** (security.md invariant #4).

Two consequences make most of the work free:

1. **"No auto-approval" costs zero code.** Any server registered in the console catalog is
   approval-gated by default; a tool auto-approves only if it is added to
   `UNCONDITIONAL_AUTO_APPROVE` in `haku/console/auto_approval.py`. We simply never add the
   exec tool there. Non-agent callers never auto-approve regardless. An arbitrary-command
   exec tool must **never** appear in that allowlist — comment it as such at the definition.
2. **The approval lifecycle, audit ledger, promise/deep-link semantics, and
   operator-identity model all already exist.** We are adding a tool server and a transport,
   not an approval system.

So the real design question is narrow: **what carries an approved command from the console
to a shell on `wyrm2`/`atlas`/…, and under what credential.**

Note: kubectl-passthrough can _already_ run commands on the three k8s-node hosts today via
`pods_exec` into a pod scheduled on the node. We are not using that because (a) it only
covers k8s nodes — not `atlas` (Proxmox host) or any non-node machine, (b) it gives no
clean `run_as agentydragon | root` semantics, and (c) it would require a standing privileged
pod on each host. A purpose-built server gives clean run-as, uniform host coverage, a proper
approval card, and host-side audit.

## Target matrix

Hosts come from the mesh roster (<../../nebula-mesh.json>); reachability from
<../../cluster/README.md> node table.

| Host     | Nebula IP    | Always on?   | k8s node? | Notes                                                  |
| -------- | ------------ | ------------ | --------- | ------------------------------------------------------ |
| `wyrm2`  | `10.42.0.20` | yes (home)   | yes       | GPU box; primary always-on target                      |
| `atlas`  | `10.42.0.5`  | yes (home)   | **no**    | Proxmox hypervisor (Debian/Ansible); root = high value |
| `rugged` | `10.42.0.30` | no (roaming) | yes       | fail-fast when offline; `destination_mtu` 1100         |
| `iguana` | `10.42.0.31` | no (roaming) | yes       | fail-fast when offline                                 |
| `pixel6` | `10.42.0.50` | —            | no        | **excluded** — no sshd                                 |

Run-as targets per host: `agentydragon` (unprivileged) and `root` (privileged). Root should
be an explicit per-host allowlist, not implicitly everywhere — `atlas` root is the
hypervisor and deserves the tightest gate.

## Transport options

### Option A — SSH over Nebula ✅ recommended

The console/cluster already reaches every target host over the Nebula overlay
(`10.42.0.0/16`), and every NixOS host already runs sshd with declaratively managed
`authorized_keys` (<../../nix/ssh-keys.nix> + each host's `openssh.authorizedKeys.keys`).
The `hostexec-mcp` pod SSHes to `10.42.0.x` and runs the command.

- **Pros:** least new machinery; **only option that covers `atlas`** and any future non-node
  host uniformly; reuses the existing sshd + declarative-key mechanism; per-host reachability
  and run-as are natural.
- **Cons:** naively implies a standing SSH key living in the pod (addressed by the credential
  ladder below); no SSH CA exists today, so a new key/identity must be added to each host.

### Option B — privileged pod-on-node (DaemonSet + `nsenter`) ❌ not primary

Because `rugged`/`wyrm2`/`iguana` are k8s nodes, a privileged `hostPID` pod pinned via
`nodeSelector: kubernetes.io/hostname` (the <../../cluster/k8s/egress-proxy-rugged/> pattern)
could `nsenter` into the host; the console reaches it with the operator's identity exactly as
kubectl-passthrough does.

- **Pros:** reuses kubectl-passthrough's operator-identity story with **no standing SSH
  credential**; nothing new to authorize in `authorized_keys`.
- **Cons:** **does not cover `atlas`** (not a node) or any non-node host; requires a
  **standing privileged/`hostPID` root pod physically resident on each host** — a larger,
  always-present blast radius than the sshd already there, and one Haku's own namespace RBAC
  must be kept far away from; roaming nodes are `NotReady` when offline. A privileged
  DaemonSet is a worse standing footprint than a signed-capability SSH path for the same
  benefit.

Keep this in mind only as a possible _add-on_ for k8s-node hosts if SSH ever proves
inadequate; it is not the base.

### Option C — bespoke host exec daemon over Nebula ❌ overkill

A small HTTP/gRPC exec daemon (systemd unit) on each host, mTLS-pinned to the console's
Nebula IP. Most control (host-side policy, structured local audit) but the most new code and
a **new standing root-capable listener to secure on every host**. Not worth it over SSH
(Option A) which already gives the transport; anything C would enforce, the L1 verifier below
enforces with ~30 lines and no new daemon.

## The credential problem (the doctrine's real test)

Option A's naive form — a standing SSH key authorized as `root` on every host, sitting in the
`hostexec-mcp` pod — is exactly the "standing credential" kubectl-passthrough was built to
avoid. Haku never holds it (the pod is in its own namespace, like the console — Haku has no
RBAC to read it), so a _prompt-injected Haku_ still can't use it without an operator click.
But **compromise of the `hostexec-mcp` pod or its Secret = silent root on every machine**, no
approval needed. That is strictly weaker than kubectl-passthrough, which holds nothing.

Three rungs, cheapest to purest:

- **L0 — standing SSH keys in the isolated pod.** Two keys (`agentydragon`, `root`) as k8s
  Secrets in the `hostexec-mcp` namespace. Harden with `from="<pod/nebula ip>"` and a
  forced-command wrapper that logs to journald. _Fastest to ship; residual = pod compromise
  is root-everywhere. Acceptable only as a throwaway MVP to validate ergonomics._

- **L1 — console-signed capability tokens.** ✅ **recommended target.** The SSH key becomes
  a dumb transport; the **authorization** is a per-call token the **console** (the trust
  boundary) signs at approval time, binding exactly `(host, run_as, argv-hash, cwd, nonce,
exp)`. A tiny verifier on each host (an `authorized_keys` `command=` / `ForceCommand`
  wrapper, ~30 lines, console public key deployed via nix) checks the signature and the bound
  fields before exec. Now a fully compromised `hostexec-mcp` pod holds only the transport — it
  **cannot produce a valid capability**, so every host refuses it; the console only signs
  after the operator approves in trusted chrome. This is the SSH analog of "the approval is
  the credential." Root is just a capability whose `run_as=root` the operator approved.
  Crux placement decision: **the console signs, not the exec server** — a signer in the exec
  server would let a compromised exec server mint its own capabilities, defeating the point.
  So the signing key lives with the console-side trust boundary; `hostexec-mcp` relays the
  signed capability + carries the SSH transport.

- **L2 — SSH certificate authority / Authentik-federated SSH.** The purest "operator's own
  identity per call" (short-lived, principal-scoped certs, or an `AuthorizedKeysCommand` that
  validates an Authentik-issued token). Biggest change — introduces an SSH CA the repo
  deliberately does **not** have today (<../../nix/ssh-keys.nix> is plain per-host keys, no
  CA) — for marginal gain over L1 with a single operator. Long-horizon only.

**Recommendation:** build straight to **L1**. L0 is the shortcut you'd regret for a root
capability; L2's SSH CA is not justified for one operator. L1 gets kubectl-passthrough's
"pod holds nothing that alone grants privilege" property with modest code and no CA.

## Recommended architecture (end to end)

```text
Haku (agent, prompt-injectable)
  │  hostexec_run {host, run_as, argv, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only)
  │  console signs capability = sig(host, run_as, sha256(argv), cwd, nonce, exp)   ← L1
  ▼
hostexec-mcp  (own namespace; SSH key = transport only, NOT the signing key)
  │  ssh <run_as>@10.42.0.x  → ForceCommand verifier
  ▼
target host: verifier checks console signature + bound fields → exec → journald/auditd log
  │  stdout/stderr/exit (capped) ──────────────────────────────────────────────┘
  ▼
result returns through console → ledger row RUNNING→done ; agent gets result or a promise
```

- **Server:** subclass `EnhancedFastMCP` like <../../mcp_infra/exec/direct.py>; reuse
  `ExecArgsBase` / `BaseExecResult` and the byte/timeout caps from
  <../../mcp_infra/exec/models.py>, adding `host` + `run_as` fields. argv is `execve`, not a
  shell — wrap in `["bash","-lc", …]` for shell features (same decision as `direct.py`). The
  remote hop (asyncssh/ssh) is the one genuinely new primitive; `run_proc` in
  `mcp_infra/exec/subprocess.py` is local-only.
- **Reachability:** fail fast on a Nebula connect timeout so a roaming/offline `rugged` returns
  a clean "host unreachable," never a hang. `wyrm2`/`atlas` are the reliable always-on targets.
- **Packaging/deploy:** copy the grocy/postscanmail Bazel `py_binary → py_image_layer →
oci_image` template and the `cluster/k8s/agents/kubectl-passthrough-mcp/app/` manifest set
  (Deployment + Service + HTTPRoute + configmap + flux-kustomization), own namespace,
  unprivileged, `automountServiceAccountToken: false`. Hostname e.g. `hostexec-mcp.allegedly.works`.
- **Register:** one entry in <../../cluster/k8s/haku/console/config.yaml> under `mcp.servers`
  (`operator_oauth` or a static bearer for reflection). Approval-gated automatically.
- **Approval card:** add a renderer under `haku/console/frontend/tool_rendering/hostexec/`
  modeled on the existing `kubectl/` renderer — show `host`, `run_as` (render `root` in red /
  behind an extra confirm), full `argv`, `cwd`, and the agent's `rationale` prominently.
- **Identity + host trust:** add a `hostexec` keypair to <../../ssh_keys/> +
  <../../nix/ssh-keys.nix>; add it to each in-scope host's `openssh.authorizedKeys.keys` for
  the `agentydragon` user and (per-host allowlist) `root`, each pinned to the L1
  `ForceCommand` verifier. `atlas` is Debian/Ansible: its keys go through the home-manager
  `home.file` path (`nix/home/hosts/atlas.nix`), and its root is the hypervisor — gate hardest.
  Private key is SOPS-encrypted and deployed only into the `hostexec-mcp` namespace Secret
  (Haku cannot read it).
- **Audit (double-entry):** console ledger `McpToolCall` (args, rationale, operator, result) +
  host-side journald/auditd from the verifier wrapper.

## What the ask maps to, concretely

- **"No auto-approval":** never add `hostexec-mcp` tools to `UNCONDITIONAL_AUTO_APPROVE`
  (`haku/console/auto_approval.py`). Every call becomes a `PENDING_APPROVAL` row needing an
  operator click. (Optionally add an extra friction for `run_as=root`: a second top-layer
  confirm or a standing "root enabled" operator toggle, honoring invariant #4.)
- **"As agentydragon or root, given approval":** the `run_as` tool field, surfaced loudly on
  the approval card; under L1 it is bound into the signed capability so the host executes only
  the exact user the operator approved.

## Open decisions for the operator

1. **Credential rung:** L1 (recommended) vs L0-first-then-L1 vs L2. Determines whether we build
   the console signer + host verifier now.
2. **Root scope:** which hosts may run `run_as=root` at all (recommend: start with `wyrm2`
   only; add `atlas` root behind an extra confirm; never roaming hosts initially).
3. **Extra root friction:** plain approval card vs a second confirm / standing root toggle.
4. **Host set for v1:** recommend `wyrm2` + `atlas` (always-on) first; add `rugged`/`iguana`
   once fail-fast reachability is proven.
5. **Server identity to the console:** `operator_oauth` (per-operator link, matches
   kubectl-passthrough) vs a static bearer.

## Risks / residuals

- **L0 residual (if chosen):** `hostexec-mcp` pod/Secret compromise = silent root everywhere.
  L1 removes it (pod holds transport, not authority).
- **Console is the trust root under L1** — as it already is for every approval-gated tool. A
  console compromise is game-over regardless; the signing key living with the console does not
  widen that.
- **Roaming hosts** (`rugged`, `iguana`) are frequently offline; treat unreachability as a
  normal, fast-failing outcome, not an error to retry indefinitely.
- **Fits the roadmap:** this is a concrete instance of PLAN.md → _"letting Haku take some
  actions itself (permission-elevation tokens)"_ — the console-signed capability is exactly the
  "scoped, expiring, per-action grant enforced by the perimeter" that section sketches.
  </content>
  </invoke>
