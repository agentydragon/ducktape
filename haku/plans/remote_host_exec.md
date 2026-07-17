# Plan: remote command execution on operator machines (Haku)

**Status:** design proposal, not built; core decisions locked (operator, 2026-07-17) — see
_Decisions_ below.
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
**Authentik** token, and a small fail-closed host-side verifier authorizes the operator's real
identity (the `hostexec-<run_as>-<host>` group claim) against Authentik's JWKS **and** checks a
console countersignature binding the exact `argv`. The `hostexec-mcp` pod holds only an inert
transport SSH key, never the authority. "May run root on `wyrm2`" is then an Authentik group you
grant/revoke centrally, exactly like cluster-admin. A privileged pod-on-node is rejected as the
primary transport (below).

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

| Host     | Nebula IP    | Always on?   | k8s node? | In scope?                       | Notes                                                          |
| -------- | ------------ | ------------ | --------- | ------------------------------- | -------------------------------------------------------------- |
| `wyrm2`  | `10.42.0.20` | yes (home)   | yes       | **yes** (`agentydragon`+`root`) | GPU box; primary always-on target                              |
| `rugged` | `10.42.0.30` | no (roaming) | yes       | **yes** (`agentydragon`+`root`) | roaming tablet; fail-fast when offline; `destination_mtu` 1100 |
| `iguana` | `10.42.0.31` | no (roaming) | yes       | deferred (TODO, 07-17)          | other roaming laptop; add later, same NixOS config as `rugged` |
| `atlas`  | `10.42.0.5`  | yes (home)   | no        | **no** (operator 07-17)         | Proxmox hypervisor; dropped from scope                         |
| `pixel6` | `10.42.0.50` | —            | no        | no                              | excluded — no sshd                                             |

v1 grants both `agentydragon` and `root` on `wyrm2` and `rugged` — one
`hostexec-{user,root}-{wyrm2,rugged}` Authentik group each. Root is an explicit per-host
allowlist (the `hostexec-root-<host>` group), not implicit. Note `rugged` is a roaming
personal tablet that travels on cellular, so its `root` grant has more physical exposure than
`wyrm2`'s — accepted (operator, 07-17); the approval card renders `root` loudly regardless.

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

### What the transport key alone grants (no theater) — decided: token verifier (operator, 07-17)

State this plainly so the approval story isn't oversold. **In this design there is no SSH key
that grants root.** The pod's transport key is `command=`-pinned to the verifier — it lands you
_in_ the verifier, not a shell — and the verifier **fail-closes unless it also receives a valid,
unexpired Authentik token** bearing the `hostexec-root-<host>` claim. Concretely:

- **Transport key at rest (exfiltrated), no token → cannot execute.** You reach a verifier that
  says no. That is the whole point of the shim, and it is the single load-bearing component: if
  the verifier is ever sloppy — key authorized directly as `root`, or not fail-closed — we are
  back to L0 and the token _is_ theater. It lives or dies on those ~30 lines.
- **What the verifier genuinely buys (not theater):** key-at-rest can't run anything; every exec
  is time-boxed (tokens expire), attributed to the Authentik identity in the token, and
  **centrally revocable** (revoke the group in Authentik → every host refuses, touching no
  `authorized_keys`).
- **The residual it does _not_ remove:** a **live, fully-compromised `hostexec-mcp` pod** holds
  the key _and_ catches operator tokens transiting it at approval time, so within a live token's
  window it could run root. The **console argv countersignature (included in v1, below)** closes
  the command-swap half of this — the relay can no longer run a _different_ command than the one
  approved. What no relay-riding design can remove: a compromised relay socially engineering the
  operator into approving a malicious command via the card. That last hop is operator judgment,
  which is the ask ("root given approval").

Bottom line: you cannot have "agent runs root given approval" without _some_ component, if
compromised at the instant of a live approval, being able to run the approved command. The job
here is not to make root impossible (root-given-approval is the ask) but to guarantee root never
happens _without_ an approval, only for the exact command approved, and is always attributed,
time-boxed, and revocable. The token verifier + argv countersignature keep all of that; L0
(below) gives them up.

### Command binding — console-countersigned argv (included in v1)

Decided (operator, 07-17): bind the **exact** command, not just who/run*as/host. At approval
time the console signs `(host, run_as, sha256(argv), cwd, nonce, exp)` with a small bespoke key
that lives with the console (the trust boundary, a Secret Haku cannot read); its public key is
deployed to each host via nix. The host verifier checks this countersignature **alongside** the
Authentik token: the Authentik token authorizes \_identity → may run_as on host* (revocable
group), the countersignature binds _this exact command, once_ (`nonce` single-use, short `exp`).
A compromised relay therefore can't swap in a different command, and can't replay a spent one.
The two checks must agree on `(host, run_as)`.

### L0 — standing keys (rejected)

Two standing SSH keys (`agentydragon`, `root`) as Secrets in the `hostexec-mcp` namespace,
`from=`-pinned + forced-command-logged. Fastest to build, but here **the key _is_ root**:
whoever holds it runs anything as root, anytime, no approval — the approval card is advisory
only against a pod compromise. Rejected (operator, 07-17) precisely to avoid that; kept here
only to name what the token verifier is buying over it.

## Recommended architecture (end to end)

```text
Haku (agent, prompt-injectable)
  │  hostexec_run {host, run_as, argv, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only)
  │  console forwards the approving operator's short-lived Authentik token (aud=hostexec)
  │  console countersigns (host, run_as, sha256(argv), cwd, nonce, exp)
  ▼
hostexec-mcp  (own namespace; unprivileged transport SSH key only — no authority of its own)
  │  ssh <run_as>@10.42.0.x  → ForceCommand/AuthorizedKeysCommand verifier
  ▼
target host: verifier validates Authentik JWT (JWKS: sig/aud/exp) + group authorizes run_as
  │            AND checks console argv countersignature (agree on host/run_as; nonce fresh)
  │            → exec → journald/auditd log
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
- **Authentik provider + groups:** add a `hostexec` OAuth2 application/provider in
  `tf/gitops/agent-machine-access` (clone the `kubectl_passthrough_mcp` provider) with a scope
  mapping that emits the operator's `hostexec-*` group claims; create the four v1 groups
  `hostexec-{user,root}-{wyrm2,rugged}` and grant all four to the operator identity.
- **Console argv-signing key:** a small signing keypair for the countersignature — private key
  a `haku-console` Secret (Haku cannot read it), minted by `tf/gitops/agent-machine-access`
  alongside the other console secrets; public key deployed to each host via nix for the
  verifier. The console signs `(host, run_as, sha256(argv), cwd, nonce, exp)` at approval time.
- **Approval card:** add a renderer under `haku/console/frontend/tool_rendering/hostexec/`
  modeled on the existing `kubectl/` renderer — show `host`, `run_as` (render `root` in red,
  behind an extra confirm), full `argv`, `cwd`, and the agent's `rationale` prominently.
- **Host trust + transport key:** deploy the host-side verifier
  (`AuthorizedKeysCommand`/`ForceCommand` shim + cached Authentik JWKS + the console
  argv-signing public key) declaratively via nix; add one **unprivileged transport** `hostexec`
  keypair (<../../ssh_keys/> + <../../nix/ssh-keys.nix>) to each in-scope host's
  `openssh.authorizedKeys.keys` for the `agentydragon` user and `root`, every entry pinned to
  `command="<verifier>"` so the key reaches nothing but the shim. Both in-scope hosts (`wyrm2`,
  `rugged`) are NixOS, so this is uniform `nix/nixos/hosts/<host>/default.nix` config — no
  `atlas`/Debian special case. The transport private key is SOPS-encrypted and deployed only
  into the `hostexec-mcp` namespace Secret (Haku cannot read it); it grants only "reach the
  verifier," never execution on its own.
- **Audit (double-entry):** console ledger `McpToolCall` (args, rationale, operator, result) +
  host-side journald/auditd from the verifier wrapper.

## What the ask maps to, concretely

- **"No auto-approval":** never add `hostexec-mcp` tools to `UNCONDITIONAL_AUTO_APPROVE`
  (`haku/console/auto_approval.py`). Every call becomes a `PENDING_APPROVAL` row needing an
  operator click. `run_as=root` renders loudly and behind a second confirm on the card (default;
  invariant #4).
- **"As agentydragon or root, given approval":** the `run_as` tool field, surfaced loudly on
  the approval card; the host executes only when the forwarded operator token's
  `hostexec-<run_as>-<host>` group authorizes exactly that user on that host **and** the console
  countersignature matches the exact `argv`.

## Decisions (operator, 2026-07-17)

1. **Credential model** — Authentik-minted operator tokens with a fail-closed host verifier;
   **no standing root key**. v1 stands up the `hostexec` Authentik provider + host JWKS verifier.
   L0 rejected.
2. **Command binding** — **include full argv**: the console countersigns the exact `argv`
   (+`host`/`run_as`/`nonce`/`exp`) so a compromised relay cannot swap or replay commands.
3. **Root scope** — `root` allowed on **both `wyrm2` and `rugged`** (four groups
   `hostexec-{user,root}-{wyrm2,rugged}`). `rugged`'s root exposure (roaming personal tablet)
   is accepted.
4. **`iguana`** — deferred; add later (TODO below).
5. **Root friction** — root renders loudly + second confirm on the approval card (default;
   adjustable).

## Future expansion (TODO)

- **`iguana`** — add to scope: one `hostexec` transport-key entry + `hostexec-{user,root}-iguana`
  groups; identical NixOS config to `rugged`, so purely additive.
- **Non-node hosts if scope grows** (e.g. `atlas`, or a future box) — SSH-over-Nebula already
  covers them uniformly; only the per-host verifier + groups are new. This is why Option A was
  chosen over the pod-on-node path.
- **Tighten the last residual** — if the "compromised relay gets a malicious command approved
  via the card" hop ever needs closing, that is fundamentally an approval-card-integrity /
  operator-judgment problem, not a transport one.

## Risks / residuals

- **Live-relay residual (narrowed, not zero):** a compromised `hostexec-mcp` pod holding a live
  forwarded token can no longer swap or replay the command (argv countersignature, decision 2) —
  it is reduced to whatever the operator literally approved. The irreducible hop is a compromised
  relay socially engineering the operator into approving a malicious command; that is the
  "root given approval" ask, mitigated by the loud root confirm.
- **The verifier is the single load-bearing component.** If the host-side shim is not
  fail-closed (or a key is authorized directly as `root`), the whole model collapses to L0.
  Treat it as security-critical, reviewed code; test the deny paths.
- **Console + Authentik are the trust roots** — as they already are for every approval-gated
  tool and all machine access. No new trust root is introduced (the console argv-signing key
  lives with the console, which is already the trust boundary).
- **Roaming hosts** (`rugged`) are frequently offline and roam on cellular (`destination_mtu`
  1100); treat unreachability as a normal, fast-failing outcome, not an error to retry
  indefinitely.
- **Fits the roadmap:** a concrete instance of PLAN.md → _"letting Haku take some actions
  itself (permission-elevation tokens)"_ — an Authentik-scoped, expiring, per-approval,
  argv-bound grant enforced by the perimeter is exactly what that section sketches.
