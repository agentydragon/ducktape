# hostexec — remote command execution on operator machines

**Ask (operator, 2026-07-17):** let Haku run shell commands on the operator's machines
(`wyrm2`, `rugged`, …) with **no auto-approval**, and the ability to run as `agentydragon`
**or `root`** once the operator approves. Core decisions are locked (see _Decisions_) and the
architecture has pivoted a few times (see _Architecture pivots_). The host daemon (`hostexecd/`)
and the console-side tool + Authentik token exchange + storage are built and tested; what's left is
deploy (Authentik providers, the host-map config, the on-node `hostexecd` unit) — see _Remaining work_.

## TL;DR

A new `hostexec` **in-process** MCP server in the console (the gmail/google_calendar pattern —
`haku/console/tools/*.py`), registered in the console catalog so it is **approval-gated by
construction**. On approval the console **exchanges the operator's identity for a short-lived,
per-host, single-use Authentik token** (the grocy token-exchange pattern) and POSTs it — with the
command — **over the cluster pod network** to a tiny host-side service (`hostexecd`). `hostexecd` is an **OIDC
resource server** (the host analog of kube-apiserver in kubectl-passthrough): it verifies the
token against Authentik's JWKS — audience `hostexec-<host>`, the `hostexec-<run_as>-<host>` group,
`exp`, and single-use (per token) — then drops privileges to `run_as`, execs, and returns output.

**Authority is the operator's own Authentik identity — no bespoke keys.** No console signing key,
no SSH key, no host-resident credential (hosts trust only Authentik's JWKS). "May run root on
`wyrm2`" is an Authentik group you grant/revoke centrally, like cluster-admin. Scope: `wyrm2` +
`rugged`, `agentydragon` + `root` on both.

## What is already decided for us

Not a green field. The repo already solved the isomorphic problem — "let a prompt-injectable
agent run arbitrary privileged operations on operator infrastructure, safely" — for `kubectl`,
and the doctrine in <../docs/security.md> fixes the shape of any answer:

- **The container/perimeter is the trust boundary, never in-agent rules.** Haku runs
  `--dangerously-skip-permissions`; the perimeter is what limits it. The exec capability cannot
  rely on Haku "behaving."
- **Execution runs under the approving operator's own authority, never a standing Haku
  credential.** kubectl-passthrough forwards the operator's own OAuth JWT straight to
  kube-apiserver (`cluster_auth_mode = "passthrough"`,
  <../../cluster/k8s/agents/kubectl-passthrough-mcp/app/configmap.yaml>); it holds no credential
  of its own. "There is no narrower RBAC backstop underneath either path" — the approval click in
  trusted console chrome is the gate. **hostexec is the same pattern**, with `hostexecd` playing
  the kube-apiserver role and a short-lived per-host token narrowing what kubectl-passthrough left
  broad.
- **Everything is in the ledger** (`McpToolCall` row, scoped to the exact principal —
  `haku/console/tool_call_service.py`), and the **approval surface is trusted chrome only**
  (security.md invariant #4).

Two consequences make most of the work free:

1. **"No auto-approval" costs zero code.** Any server in the console catalog is approval-gated
   by default; a tool auto-approves only if added to `UNCONDITIONAL_AUTO_APPROVE`
   (`haku/console/auto_approval.py`). We never add the exec tool there — comment it as such at
   the definition. Non-agent callers never auto-approve regardless.
2. **The approval lifecycle, audit ledger, promise/deep-link semantics, operator-identity model,
   and token exchange all already exist.** We add a tool server + a host service.

So the real design question is narrow: **what carries an approved command to a shell on
`wyrm2`/`rugged`, and under what credential.**

## Transport: a host-side OIDC exec service, not SSH

Once the host authenticates the operator's Authentik token, it **is** an OIDC resource server,
and the natural transport is a small authenticated HTTP endpoint — not sshd + a `ForceCommand`
shim.

The in-process console tool POSTs `{token, run_as, argv, cwd, timeout_ms}` **over the cluster pod
network** to `hostexecd` on the target node — the machines are k8s nodes, addressed by
`kubernetes.io/hostname` (`wyrm2`, `rugged`). `hostexecd` runs as **root in the host's namespaces**,
validates the Authentik JWT against cached JWKS, drops privileges to `run_as`, execs argv (`execve`,
no shell), and returns a capped `BaseExecResult`.

- **No SSH key, no host-resident credential.** Hosts trust only Authentik's public JWKS; there is
  no per-host key to steal and no "is the SSH key root?" surface.
- **Natural payload + clean `run_as`.** Structured JSON, and privilege-drop happens once,
  centrally, inside `hostexecd` (root → `setuid`/`runuser` to the approved user) instead of via
  sshd login-user + `sudo` gymnastics.
- **Channel security from a network policy, not Nebula.** The machines are already k8s nodes, so
  the console reaches `hostexecd` on the pod network — no mesh egress to plumb. A
  `CiliumNetworkPolicy` restricts ingress to the `haku-console` pod (namespace + pod-label), and the
  fail-closed token check backstops it — the two are defense-in-depth.
  (`egress-proxy-rugged`'s `networkpolicy.yaml` is the precedent.)

**Decided (operator, 2026-07-18): a systemd unit on the host** (not the DaemonSet), on **both
`wyrm2` and `rugged`**. It runs natively on the host, so `exec.rs`/`users.rs` work as-is (`getpwnam`
plus the full setgroups/setgid/setuid drop) with no chroot/nsenter host-breakout — the DaemonSet
would have had to break back out of the container onto the host (host-passwd resolution and a
chroot+drop `pre_exec`), a re-architecture we avoid. Smallest root TCB (Decision #3); the cost is
packaging.

Packaging follows the repo's **prebuilt-artifact** pattern, not a nix Cargo rebuild: Bazel builds
`//haku/hostexec/hostexecd:hostexecd`, CI publishes it as a GitHub release, `nix/artifact-pins.json`
pins it (url + sha256), and a NixOS module fetches + runs it — exactly how `bbapi`/`bbr` reach the
hosts today. So there is **no** second (Cargo) build definition to maintain; nix consumes the Bazel
binary. `hostexecd` listens on a node-reachable port; a host firewall (nftables) restricts ingress
to `haku-console`. exec_url = the node's cluster IP (the mesh IP for these roaming workers) : port.

The rejected DaemonSet alternative was **admissible** — the choice was TCB-minimization, not whether
the cluster allows it
(investigated 2026-07-18): `wyrm2`/`rugged` are **NixOS** worker nodes, not Talos (Talos is only the
OVH/Proxmox control plane), so Talos host-hardening never touches the exec targets. The one gate is
Pod Security Admission — Talos's built-in `baseline` default blocks `privileged`/`hostPID`, but a
namespace labeled `pod-security.kubernetes.io/enforce: privileged` clears it, exactly as the
`privileged: true` `kvm-device-plugin` DaemonSet (hostPath `/dev`) and root+`hostNetwork`+`NET_ADMIN`
`cpap-sync` already run on `wyrm2`. Kyverno doesn't restrict privileged/hostPID (its one workload
policy is Audit-mode and excludes Flux). So the DaemonSet path is viable — it just adds the
container/k8s surface around the same root-on-host the systemd unit has.

Rejected: **SSH** — once auth is the Authentik token it only adds a standing transport key and
`SSH_ORIGINAL_COMMAND` plumbing, for no benefit.

## Authorization model: Authentik-native narrowing (no bespoke keys)

The baseline is kubectl-passthrough: forward the operator's token, and if the resource server
"likes" it (right group), you may act. That token is _broad_ — possession = any root command on
the host until it expires. We narrow it as far as possible **using Authentik's own machinery**, so
authority stays the operator's real identity (revocable, no bespoke standing key). Each rung and
where it comes from:

| Rung              | Narrows a possessed token to…  | Source                                                                  |
| ----------------- | ------------------------------ | ----------------------------------------------------------------------- |
| host              | …only `wyrm2`, not `rugged`    | Authentik: per-host provider, `aud=hostexec-<host>`                     |
| run_as            | …only `root`, not any user     | Authentik: the `hostexec-<run_as>-<host>` group claim                   |
| time              | …the next few seconds          | Authentik: short access-token TTL on the exchanged token                |
| single-use        | …exactly once                  | `hostexecd`: a per-token replay store (reject an already-seen token)    |
| _(not done)_ argv | …only the one approved command | would need a bespoke console-signed capability — deliberately not built |

The first four rungs are all Authentik-native (**token exchange** to a **per-host provider** —
reuse `mcp_infra/authentik_auth/token_exchange.py`, the grocy pattern) plus a small host-side
replay store. `hostexecd` verifies: signature against Authentik JWKS, `aud=hostexec-<host>`, `exp`,
the `hostexec-<run_as>-<host>` group, and the token unseen (single-use) — then execs.

What this buys (not theater):

- **No bespoke standing key anywhere.** No console signing key, no host key. A console compromise
  can drive token exchange _as the operator_ — bounded by the operator's Authentik grants and
  cut instantly by removing the group — but there is no root-capable skeleton key to steal.
- **A stolen token is nearly worthless.** It is scoped to one host, one `run_as`, valid for
  seconds, and **consumed on first use** (single-use) — so a leaked-but-not-yet-used token
  is exploitable only in the millisecond window between mint and the legitimate call.
- **Time-boxed, attributed, centrally revocable.** Every exec is tied to the operator's Authentik
  identity and logged; revoke the group → every host refuses.

Why we stopped before the argv rung: the only thing argv-binding adds is shrinking a stolen
token from "any root command on host X for the TTL" to "the one approved command." With short TTL

- single-use that residual is already a sub-second window, and against real compromise (injected
  Haku can't hold a token; a compromised console can mint either way) it buys nothing. Not worth a
  bespoke signing key + custom capability crypto. (See _Architecture pivots_ — this was built, then
  dropped.)

The load-bearing component and the residual:

- **`hostexecd`'s fail-closed validation is the single load-bearing component.** If it is sloppy
  (accepts a missing/expired token, skips the group or single-use check, mis-drops privileges) the
  model collapses. Security-critical reviewed code; test the deny paths.
- **The console is the trust root** (it holds the operator's linkage and drives token exchange) —
  as it already is for every approval-gated tool. A console compromise yields host access, but
  that is the same trust root, not a new one. The irreducible hop no design removes:
  social-engineering the operator into approving a malicious command via the card — which is the
  "root given approval" ask itself, mitigated by the operator reading the full command on the
  approval card before approving.

## Target matrix

Hosts are **k8s nodes**, addressed by `kubernetes.io/hostname` and reached over the **cluster pod
network** (the Nebula IPs from the mesh roster <../../nebula-mesh.json> are shown for reference only
— the console no longer addresses hosts by them). Reachability: <../../cluster/README.md>.

| Host     | k8s node (`hostname`) | Nebula IP    | In scope?                       | Notes                                            |
| -------- | --------------------- | ------------ | ------------------------------- | ------------------------------------------------ |
| `wyrm2`  | `wyrm2`               | `10.42.0.20` | **yes** (`agentydragon`+`root`) | GPU box; primary always-on node                  |
| `rugged` | `rugged`              | `10.42.0.30` | **yes** (`agentydragon`+`root`) | roaming tablet node; fail-fast when off-cluster  |
| `iguana` | `iguana`              | `10.42.0.31` | deferred (TODO, 07-17)          | same NixOS config as `rugged`; add later         |
| `atlas`  | —                     | `10.42.0.5`  | **no** (operator 07-17)         | Proxmox hypervisor, not a k8s node; out of scope |
| `pixel6` | —                     | `10.42.0.50` | no                              | excluded — no host we run a service on           |

v1 creates a per-host Authentik provider (`hostexec-wyrm2`, `hostexec-rugged`) and the four groups
`hostexec-{user,root}-{wyrm2,rugged}`, all granted to the operator identity. `root` is an explicit
per-host group. `rugged`'s `root` grant has more physical exposure (roaming tablet on cellular) —
accepted (operator, 07-17).

## Architecture (end to end)

```text
Haku (agent, prompt-injectable)
  │  hostexec_run {host, run_as, cmd, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only)
  │  in-process `hostexec` tool executes in the console:
  │    · token-exchange the operator's identity → short-lived token (aud=hostexec-<host>, groups)
  │    · POST over the cluster pod network → hostexecd on node <host> (Service/hostPort) /exec
  ▼
hostexecd on target node (root, host namespaces; on the pod network, CiliumNetworkPolicy → only haku-console):
  │  verify token vs Authentik JWKS: sig, aud=hostexec-<host>, exp, group hostexec-<run_as>-<host>
  │  AND token not already used (single-use replay store)
  │  → drop privileges to run_as → execve(argv) → journald/auditd log
  │  capped stdout/stderr/exit ────────────────────────────────────────────┘
  ▼
result returns through the console → ledger row RUNNING→done ; agent gets result or a promise
```

## Remaining work

- **In-process `hostexec` console MCP server — built and tested.** The `hostexec_run` tool
  (`haku/console/tools/hostexec.py`, flat `host` + `run_as` + exec args), its `HostexecClient`
  (POST `HostexecRequest` to `hostexecd`, return `BaseExecResult`), and the concrete
  `HostexecJwtBearerExchanger` (RFC-7523 jwt-bearer exchange of the operator's Authentik token → per-host
  `aud=hostexec-<host>`) are done. Option A's storage is done: login gains `offline_access` (only
  when hostexec is configured), `PostgresAuthentikOperatorTokenStore` persists + self-refreshes the
  operator's Authentik token, which `backend_auth_for_operator` resolves for the `OperatorIdentityAuth`
  variant; the in-process builder is wired from `settings.hostexec`. **Remaining is deploy-only:** the
  host map (`HAKU_CONSOLE_HOSTEXEC` JSON: `exec_url` + `audience_client_id` per host) and the catalog
  entry (`{id: hostexec, auth: {kind: operator_identity}}`) in
  <../../cluster/k8s/haku/console/config.yaml> — both coupled to the Authentik providers below, so
  they land together at deploy. The `haku-console` pod reaches `hostexecd` over the **cluster pod
  network** by node hostname (no mesh egress); a `CiliumNetworkPolicy` restricts hostexecd's ingress.
- **`hostexecd` daemon — done.** The Rust daemon (`hostexecd/`, `axum` + `jsonwebtoken`; verify →
  resolve `run_as` → single-use claim → full setgroups/setgid/setuid drop → `execve`) is built and
  tested, including the supplementary-groups drop (was the `initgroups` gap) and the approval + exit
  audit lines (journald under systemd). **Remaining is deploy-only** (systemd path, see _Transport_):
  (1) a **CI publish** step — **added** (`hostexecd` in `devinfra/ci/artifact_targets.json`); on
  merge, `release.yml` publishes the Bazel-built binary and `sync-pins.yml` auto-updates the
  **`nix/artifact-pins.json`** pin (url + sha256) within ~30 min. Remaining: (2) a **NixOS module**
  that fetches the pinned binary + runs it as a root systemd service (env:
  `HOSTEXEC_{HOST,ISSUER,JWKS_URL,BIND}`) with an **nftables** rule restricting the port to
  `haku-console`; (3) **enable it on `wyrm2` + `rugged`**. Both remain untestable in this environment
  (needs the actual hosts) and land as one operator deploy together with the console host-map +
  catalog entry above.
- **Authentik providers + groups — written** (`tf/gitops/agent-machine-access/hostexec.tf`): per-host
  `hostexec-<host>` confidential OAuth2 providers (aud = client_id, 1-min TTL, RS256 self-signed key),
  a `groups` scope mapping emitting the operator's `hostexec-*` groups, the four
  `hostexec-{user,root}-{wyrm2,rugged}` groups on the operator, and the token-exchange trust
  (`jwt_federation_providers = [haku_console_operator]`); plus `offline_access` added to the
  `haku-console` operator-login provider so the console gets a refresh token. **Remaining: apply** —
  the tofu-controller reconciles this on merge (checkov + tflint pass; a full `tofu validate` needs
  registry access this env blocks, so the controller is the first real provider-schema check).

A dedicated approval-card renderer is **not** required to ship: hostexec calls render with the
generic tool-call card (host, run*as, argv, cwd, rationale are all visible), and there is no
second-confirm on `root` (operator, 07-18). A bespoke renderer + loud `root` treatment is a deferred
maybe — see \_Future expansion*.

## Decisions (operator, 2026-07-17)

1. **Authorization** — the operator's own **Authentik** token, verified by a fail-closed host
   service; **no bespoke standing key** (no SSH key, no console signing key). A standing-SSH-key
   variant was rejected.
2. **Transport** — a **host-side OIDC exec service (`hostexecd`)** reached over the cluster network
   (the machines are k8s nodes); not SSH, not over Nebula addressing. **Runs as a NixOS systemd unit
   on the host** (operator, 07-18) on both `wyrm2` + `rugged`, ingress restricted by an nftables
   rule to `haku-console`; the privileged-DaemonSet alternative was rejected to avoid a
   chroot/nsenter host-breakout and keep the smallest root TCB.
3. **`hostexecd` language** — minimal **Rust** (smallest root TCB, no interpreter on the host).
4. **Narrowing** — **Authentik-native**: per-host provider (`aud=hostexec-<host>`) + the
   `hostexec-<run_as>-<host>` group + short token TTL + host-side per-token single-use. **No
   argv-level binding / no bespoke console-signed capability** — the marginal hardening over
   short-TTL + single-use doesn't justify a bespoke signing key (superseded the earlier "full
   argv" decision; see _Architecture pivots_).
5. **In-process, not a separate MCP server** — the `hostexec` tool runs in-process in the console
   (gmail/google_calendar pattern). Console→mesh egress is the only thing to plumb.
6. **Root scope** — `root` on **both `wyrm2` and `rugged`**. `rugged`'s exposure accepted.
7. **`iguana`** — deferred (TODO below).
8. **Root friction** — none for now (operator, 07-18): no second-confirm on `root`. A loud
   approval-card treatment for `root` is a deferred maybe (see _Future expansion_).

## Architecture pivots

Kept as a record so the reasoning survives the code churn.

- **SSH-over-Nebula → host-side OIDC service (`hostexecd`).** Once auth is the Authentik token,
  the host is an OIDC resource server; SSH only added a standing transport key and
  `SSH_ORIGINAL_COMMAND` plumbing for no benefit.
- **Separate remote `hostexec-mcp` pod → in-process console tool.** The minting/exchange is custom
  code that belongs at the console (the trust boundary); a separate pod would hold the credential
  (worse) or need the console to inject it anyway (no gain).
- **Nebula transport → cluster pod network + network policy.** The target machines are already k8s
  nodes (`kubernetes.io/hostname`), so the console reaches `hostexecd` over the pod network and a
  `CiliumNetworkPolicy` restricts ingress to `haku-console` — dropping the console→mesh egress
  plumbing and the `nebula1` bind. `hostexecd` still `execve`s in the host's namespaces (systemd
  unit or privileged DaemonSet — deploy-time). Nebula IPs remain the mesh roster's identity, just
  not how the console addresses hosts.
- **Bespoke console-signed capability (argv-bound) → Authentik-native narrowing.** Built the
  capability first (Python `capability.py` + Rust `capability.rs` + cross-language JWT vectors),
  then dropped it: per-host provider + short TTL + single-use gets the token nearly as narrow
  (host/run_as/time/one-shot) using the operator's real Authentik identity and existing
  token-exchange machinery, with **no bespoke standing key**. Argv-binding's only extra is
  shrinking a leaked-but-unused token's window from "TTL" to "one command" — sub-second given
  single-use, and worthless against real compromise.

## Future expansion (TODO)

- **`iguana`** — add: a `hostexec-iguana` provider + `hostexec-{user,root}-iguana` groups + the
  `hostexecd` unit; identical NixOS config to `rugged`, purely additive.
- **Bespoke approval-card renderer + `root` friction** (maybe) — a `hostexec/` renderer under
  `haku/console/frontend/tool_rendering/` (modeled on `kubectl/`) that highlights `host` / `run_as`
  and could add a loud treatment or second-confirm for `root`. Deferred (operator, 07-18): the
  generic tool-call card already shows the full call, and no second-confirm is wanted for now.
- **Non-node hosts** — pod-network reach covers k8s nodes only; every current in-scope host is a
  node. A future non-node mesh host would need its own reachability (e.g. Nebula) or to join the
  cluster.
- **Tighten the last residual** — closing "socially engineer the operator into approving a
  malicious command via the card" is an approval-card-integrity / operator-judgment problem, not a
  transport one.

## Risks / residuals

- **The console is the trust root** (holds the operator linkage, drives token exchange) — as it
  already is for every approval-gated tool. A console compromise yields host access bounded by the
  operator's Authentik grants and revocation; there is no root-capable standing key to steal.
- **`hostexecd` is the single load-bearing component** and a root-capable network service. Keep it
  minimal, network-policy-restricted to `haku-console`, fail-closed, privilege-dropping,
  argv-`execve` (no shell); test the deny paths as security-critical.
- **Roaming hosts** (`rugged`) are frequently offline; treat unreachability as a normal,
  fast-failing outcome, not an error to retry indefinitely.
- **Fits the roadmap:** a concrete instance of <../PLAN.md> → _"letting Haku take some actions
  itself (permission-elevation tokens)"_ — an Authentik-scoped, expiring, single-use, per-host
  grant enforced by the perimeter is exactly what that section sketches.
