# Plan: remote command execution on operator machines (Haku)

**Status:** design proposal, not built (operator, 2026-07-17).
**Ask:** let Haku run shell commands on the operator's machines (`wyrm2`, `rugged`,
`iguana`, …) with **no auto-approval**, and the ability to run as `agentydragon`
**or `root`** once the operator approves. Which transport — SSH over Nebula, a privileged
pod, something else? (Scope refined 07-17: `rugged` in, `atlas` out.)

## TL;DR recommendation

Build a new remote MCP server `hostexec-mcp` modeled on
<../../cluster/k8s/agents/kubectl-passthrough-mcp/>, registered in the console catalog so
it is approval-gated by construction. **Transport: SSH over the existing Nebula mesh** —
it reuses machinery that already exists (sshd + declarative `authorized_keys` on every host)
and needs **no standing privileged pod**, unlike the pod-on-node alternative (which is why
it wins even though every in-scope host — `wyrm2`, `rugged`, `iguana` — is now a k8s node).
**Authorization = Authentik, not a standing key:** make it
"kubectl-passthrough for SSH" — the console forwards the approving operator's short-lived
**Authentik** token, and a small host-side verifier authorizes the operator's real identity
(the `hostexec-<run_as>-<host>` group claim) against Authentik's JWKS. The `hostexec-mcp` pod
holds only an inert transport SSH key, never the authority. "May run root on `wyrm2`" is then
an Authentik group you grant/revoke centrally, exactly like cluster-admin. A privileged
pod-on-node is rejected as the primary transport (below).

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
to a shell on `wyrm2`/`rugged`/…, and under what credential.**

Note: with all in-scope hosts being k8s nodes, kubectl-passthrough can _already_ run commands
on them today via `pods_exec` into a pod scheduled on the node — the lowest-effort path. We
still don't, because it (a) gives no clean `run_as agentydragon | root` semantics (nsenter +
setuid gymnastics), (b) needs a **standing privileged/`hostPID` pod on each node** — including
the roaming personal tablet `rugged` — which is a larger always-resident root footprint than
the sshd already there, and likely trips pod-security/Kyverno anyway, and (c) yields a weaker
approval card and audit trail. A purpose-built SSH server gives clean run-as, no standing
privileged pod, a proper approval card, and host-side audit.

## Target matrix

Hosts come from the mesh roster (<../../nebula-mesh.json>); reachability from
<../../cluster/README.md> node table.

| Host     | Nebula IP    | Always on?   | k8s node? | In scope?               | Notes                                                          |
| -------- | ------------ | ------------ | --------- | ----------------------- | -------------------------------------------------------------- |
| `wyrm2`  | `10.42.0.20` | yes (home)   | yes       | **yes**                 | GPU box; primary always-on target                              |
| `rugged` | `10.42.0.30` | no (roaming) | yes       | **yes**                 | roaming tablet; fail-fast when offline; `destination_mtu` 1100 |
| `iguana` | `10.42.0.31` | no (roaming) | yes       | same class as `rugged`  | other roaming laptop; include unless told otherwise            |
| `atlas`  | `10.42.0.5`  | yes (home)   | no        | **no** (operator 07-17) | Proxmox hypervisor; dropped from scope                         |
| `pixel6` | `10.42.0.50` | —            | no        | no                      | excluded — no sshd                                             |

Run-as targets per host: `agentydragon` (unprivileged) and `root` (privileged). Root is an
explicit per-host allowlist (a `hostexec-root-<host>` group), not implicitly everywhere.

**Consequence of dropping `atlas`:** every in-scope host is now a k8s node. That removes the
one host that _forced_ SSH over a privileged pod — so the transport choice is re-justified on
its remaining merits below rather than on "only SSH reaches `atlas`."

## Transport options

### Option A — SSH over Nebula ✅ recommended

The console/cluster already reaches every target host over the Nebula overlay
(`10.42.0.0/16`), and every NixOS host already runs sshd with declaratively managed
`authorized_keys` (<../../nix/ssh-keys.nix> + each host's `openssh.authorizedKeys.keys`).
The `hostexec-mcp` pod SSHes to `10.42.0.x` and runs the command.

- **Pros:** no standing privileged pod anywhere (the exec pod stays unprivileged); reuses the
  existing sshd + declarative-key mechanism; clean per-host reachability and `run_as`; covers
  any future non-node host uniformly if scope ever grows back.
- **Cons:** naively implies a standing SSH key living in the pod (addressed by the credential
  ladder below); no SSH CA exists today, so a new key/identity must be added to each host.

### Option B — privileged pod-on-node (DaemonSet + `nsenter`) ❌ not primary

Because `rugged`/`wyrm2`/`iguana` are k8s nodes, a privileged `hostPID` pod pinned via
`nodeSelector: kubernetes.io/hostname` (the <../../cluster/k8s/egress-proxy-rugged/> pattern)
could `nsenter` into the host; the console reaches it with the operator's identity exactly as
kubectl-passthrough does.

- **Pros:** reuses kubectl-passthrough's operator-identity story with **no standing SSH
  credential**; nothing new to authorize in `authorized_keys`.
- **Cons:** requires a **standing privileged/`hostPID` root pod physically resident on each
  node** — including the roaming personal tablet `rugged` — a larger, always-present blast
  radius than the sshd already there, likely blocked by pod-security/Kyverno, and one Haku's
  own namespace RBAC must be kept far away from; roaming nodes are `NotReady` when offline;
  and it covers only k8s nodes, so scope could never grow to a non-node host. A worse standing
  footprint than the SSH path for the same benefit.

Keep this in mind only as a possible _add-on_ for k8s-node hosts if SSH ever proves
inadequate; it is not the base.

### Option C — bespoke host exec daemon over Nebula ❌ overkill

A small HTTP/gRPC exec daemon (systemd unit) on each host, mTLS-pinned to the console's
Nebula IP. Most control (host-side policy, structured local audit) but the most new code and
a **new standing root-capable listener to secure on every host**. Not worth it over SSH
(Option A) which already gives the transport; anything C would enforce, the host-side verifier
below enforces with ~30 lines and no new daemon.

## The credential problem (the doctrine's real test)

Option A's naive form — a standing SSH key authorized as `root` on every host, sitting in the
`hostexec-mcp` pod — is exactly the "standing credential" kubectl-passthrough was built to
avoid. Haku never holds it (the pod is in its own namespace, like the console — Haku has no
RBAC to read it), so a _prompt-injected Haku_ still can't use it without an operator click.
But **compromise of the `hostexec-mcp` pod or its Secret = silent root on every machine**, no
approval needed. That is strictly weaker than kubectl-passthrough, which holds nothing.

The right question is not "how do we protect a signing key" but **where the authority to
authorize a command lives**. kubectl-passthrough's answer is: in the operator's own Authentik
identity — the console forwards the operator's short-lived Authentik OAuth token, and
kube-apiserver enforces the operator's Authentik-group RBAC. **We should do the same for
hostexec: make Authentik the token issuer, so the host authorizes the operator's real
identity, not a key the exec pod holds.** This is "kubectl-passthrough for SSH."

### Recommended — Authentik-minted operator tokens ("passthrough for SSH") ✅

The authority to run as `root`/`agentydragon` on a given host is an **Authentik group
membership**, minted and revoked centrally in `tf/gitops/agent-machine-access` exactly like
the `kubectl_passthrough_mcp` provider (<../../cluster/k8s/haku/console/config.yaml> lines
32–44 describe that provider). Flow:

1. The operator links Authentik to the console once (`operator_oauth`, the existing
   mechanism), scoped to a new `hostexec` Authentik application/provider whose scope mapping
   emits the operator's `hostexec-*` group claims.
2. On approval, the console forwards **that approving operator's** short-lived Authentik
   access token (audience `hostexec`) to `hostexec-mcp` — the same per-operator token store
   and forwarding kubectl-passthrough already uses.
3. `hostexec-mcp` presents the token to the host over the SSH channel; a small host-side
   **verifier** (an `AuthorizedKeysCommand` / `ForceCommand` shim, deployed by nix, with
   Authentik's public JWKS cached locally) validates signature + audience + expiry and checks
   the group claim authorizes the requested `run_as` on this host — then execs, logging to
   journald/auditd.

Why this is the right shape:

- **No bespoke signing key and no SSH CA.** The signer is Authentik (which already issues the
  JWTs kube-apiserver federates); the host is just another OIDC resource server. The
  `hostexec-mcp` pod holds only an unprivileged **transport** SSH key that reaches the
  forced-command shim and nothing else — the _authority_ is the forwarded operator token,
  short-lived and per-approval.
- **Authority is managed where all machine-access already is.** "May run root on `wyrm2`"
  becomes an Authentik group you grant/revoke/audit centrally, with the same lifecycle as
  cluster-admin. Different operator identities can carry different exec rights (e.g. only the
  human operator's identity is in `hostexec-root-wyrm2`) — future-proof even though there's
  one operator today.
- **Trust level equals kubectl-passthrough's** — no better, no worse. A compromised
  `hostexec-mcp` pod holding a live forwarded token could, in the window, run a _different_
  command as the approved `run_as` (just as a compromised kubectl-passthrough pod holding a
  forwarded cluster-admin token could make a different API call). The repo already accepts
  that residual for the reviewed relay + short-TTL token; hostexec inherits the same bargain.

**Honest limitation:** an Authentik OIDC token naturally encodes _who / run_as / host / TTL_,
not the exact `argv` (OIDC claims come from user/group attributes, not per-request input). So
it authorizes "this operator may run root on `wyrm2` for the next N seconds," not "…may run
exactly _this_ command once." That is the same granularity kubectl-passthrough has.

### Optional hardening — console-countersigned argv binding

If you want hostexec **stronger** than kubectl-passthrough (defend against a compromised relay
swapping the command), add a second, small per-approval assertion: the console signs
`sha256(argv) + nonce + exp` at approval time and the host verifier checks it alongside the
Authentik token. This is a tiny bespoke key on the console side (the trust boundary), binding
the _exact command_ the operator approved. Additive — layer it only if the relay-swap residual
matters to you.

### L0 — standing keys (throwaway MVP only)

Two standing SSH keys (`agentydragon`, `root`) as Secrets in the `hostexec-mcp` namespace,
`from=`-pinned + forced-command-logged. Fastest to stand up to validate ergonomics; residual =
pod compromise is root-everywhere. Only as a scaffold you replace with the Authentik-token
path before this is trusted with root.

## Recommended architecture (end to end)

```text
Haku (agent, prompt-injectable)
  │  hostexec_run {host, run_as, argv, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only)
  │  console forwards the approving operator's short-lived Authentik token (aud=hostexec)
  │  [optional] console countersigns sha256(argv)+nonce+exp
  ▼
hostexec-mcp  (own namespace; unprivileged transport SSH key only — no authority of its own)
  │  ssh <run_as>@10.42.0.x  → ForceCommand/AuthorizedKeysCommand verifier
  ▼
target host: verifier validates Authentik JWT (JWKS: sig/aud/exp) + group authorizes run_as
  │            [optional] checks console argv countersignature → exec → journald/auditd log
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
- **Reachability:** fail fast on a Nebula connect timeout so a roaming/offline `rugged`/`iguana`
  returns a clean "host unreachable," never a hang. `wyrm2` is the reliable always-on target;
  `rugged`/`iguana` are in scope but frequently offline, so unreachability is a normal outcome.
- **Packaging/deploy:** copy the grocy/postscanmail Bazel `py_binary → py_image_layer →
oci_image` template and the `cluster/k8s/agents/kubectl-passthrough-mcp/app/` manifest set
  (Deployment + Service + HTTPRoute + configmap + flux-kustomization), own namespace,
  unprivileged, `automountServiceAccountToken: false`. Hostname e.g. `hostexec-mcp.allegedly.works`.
- **Register:** one entry in <../../cluster/k8s/haku/console/config.yaml> under `mcp.servers`
  with `operator_oauth` (matches kubectl-passthrough — the console forwards the approving
  operator's Authentik token). Approval-gated automatically.
- **Authentik provider:** add a `hostexec` OAuth2 application/provider in
  `tf/gitops/agent-machine-access` (clone the `kubectl_passthrough_mcp` provider) with a scope
  mapping that emits the operator's `hostexec-*` group claims; create the `hostexec-{user,root}-<host>`
  groups and grant them to the operator identity per the root-scope decision below.
- **Approval card:** add a renderer under `haku/console/frontend/tool_rendering/hostexec/`
  modeled on the existing `kubectl/` renderer — show `host`, `run_as` (render `root` in red /
  behind an extra confirm), full `argv`, `cwd`, and the agent's `rationale` prominently.
- **Host trust + transport key:** deploy the host-side verifier
  (`AuthorizedKeysCommand`/`ForceCommand` shim + cached Authentik JWKS) declaratively via nix;
  add one **unprivileged transport** `hostexec` keypair (<../../ssh_keys/> + <../../nix/ssh-keys.nix>)
  to each in-scope host's `openssh.authorizedKeys.keys` for the `agentydragon` user and
  (per-host allowlist) `root`, every entry pinned to `command="<verifier>"` so the key reaches
  nothing but the shim. All in-scope hosts (`wyrm2`, `rugged`, `iguana`) are NixOS, so this is
  uniform `nix/nixos/hosts/<host>/default.nix` config — no `atlas`/Debian special case. The
  transport private key is SOPS-encrypted and deployed only into the `hostexec-mcp` namespace
  Secret (Haku cannot read it); it grants only "reach the verifier," never execution on its own.
- **Audit (double-entry):** console ledger `McpToolCall` (args, rationale, operator, result) +
  host-side journald/auditd from the verifier wrapper.

## What the ask maps to, concretely

- **"No auto-approval":** never add `hostexec-mcp` tools to `UNCONDITIONAL_AUTO_APPROVE`
  (`haku/console/auto_approval.py`). Every call becomes a `PENDING_APPROVAL` row needing an
  operator click. (Optionally add an extra friction for `run_as=root`: a second top-layer
  confirm or a standing "root enabled" operator toggle, honoring invariant #4.)
- **"As agentydragon or root, given approval":** the `run_as` tool field, surfaced loudly on
  the approval card; the host verifier executes it only if the forwarded operator token's
  `hostexec-<run_as>-<host>` group authorizes exactly that user on that host.

## Open decisions for the operator

1. **Credential model:** Authentik-minted operator tokens (recommended) vs L0 standing keys
   first. Determines whether v1 stands up the `hostexec` Authentik provider + host JWKS
   verifier now or after an ergonomics MVP.
2. **argv binding:** ship at kubectl-passthrough parity (Authentik token only), or add the
   console argv countersignature to defend against a compromised relay swapping the command.
3. **Root scope:** which in-scope hosts get a `hostexec-root-<host>` group at all. Open
   question: `rugged` (the operator's own tablet) is in scope for exec — is `run_as=root`
   wanted there too, or `agentydragon`-only on `rugged` with root reserved to `wyrm2`?
4. **Extra root friction:** plain approval card vs a second confirm / standing root toggle.
5. **`iguana`:** in with `rugged` (same roaming-laptop class, uniform NixOS config) unless the
   operator wants it left out.

## Risks / residuals

- **L0 residual (if chosen):** `hostexec-mcp` pod/Secret compromise = silent root everywhere.
  The Authentik-token path removes it (pod holds only an inert transport key; authority is the
  short-lived forwarded operator token).
- **Relay-swap residual (parity with kubectl-passthrough):** a compromised `hostexec-mcp` pod
  holding a live forwarded token could, in its short window, run a different command as the
  approved `run_as`. Accepted for kubectl-passthrough today; the optional argv countersignature
  (decision 2) closes it for hostexec.
- **Console + Authentik are the trust roots** — as they already are for every approval-gated
  tool and all machine access. No new trust root is introduced.
- **Roaming hosts** (`rugged`, `iguana`) are frequently offline and roam on cellular
  (`rugged` at `destination_mtu` 1100); treat unreachability as a normal, fast-failing
  outcome, not an error to retry indefinitely.
- **Fits the roadmap:** a concrete instance of PLAN.md → _"letting Haku take some actions
  itself (permission-elevation tokens)"_ — an Authentik-scoped, expiring, per-approval grant
  enforced by the perimeter is exactly what that section sketches.
