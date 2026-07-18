# hostexec — remote command execution on operator machines

**Ask (operator, 2026-07-17):** let Haku run shell commands on the operator's machines
(`wyrm2`, `rugged`, …) with **no auto-approval**, and the ability to run as `agentydragon`
**or `root`** once the operator approves. Core decisions are locked (see _Decisions_) and the
architecture has pivoted a few times (see _Architecture pivots_); the exec module + wire tool
input exist, the rest is remaining work below.

## TL;DR

A new `hostexec` **in-process** MCP server in the console (the gmail/google_calendar pattern —
`haku/console/tools/*.py`), registered in the console catalog so it is **approval-gated by
construction**. On approval the console **exchanges the operator's identity for a short-lived,
per-host, single-use Authentik token** (the grocy token-exchange pattern) and POSTs it — with the
command — **over Nebula** to a tiny host-side service (`hostexecd`). `hostexecd` is an **OIDC
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

The in-process console tool POSTs `{token, run_as, argv, cwd, timeout_ms}` over Nebula to
`hostexecd` on the target host. `hostexecd` runs as root (systemd), validates the Authentik JWT
against cached JWKS, drops privileges to `run_as`, execs argv (`execve`, no shell), and returns a
capped `BaseExecResult`.

- **No SSH key, no host-resident credential.** Hosts trust only Authentik's public JWKS; there is
  no per-host key to steal and no "is the SSH key root?" surface.
- **Natural payload + clean `run_as`.** Structured JSON, and privilege-drop happens once,
  centrally, inside `hostexecd` (root → `setuid`/`runuser` to the approved user) instead of via
  sshd login-user + `sudo` gymnastics.
- **Channel security from Nebula.** Nebula already provides encryption + peer identity; bind
  `hostexecd` to `nebula1`, firewall it to the console's mesh peer, and the token requirement
  backstops reachability.

Rejected: **SSH** — once auth is the Authentik token it only adds a standing transport key and
`SSH_ORIGINAL_COMMAND` plumbing, for no benefit. **Privileged pod-on-node** — a standing `hostPID`
root pod on each node (incl. the roaming tablet `rugged`), likely blocked by pod-security/Kyverno,
covering only k8s nodes; `hostexecd`-over-Nebula covers any mesh host
(<../../cluster/k8s/egress-proxy-rugged/> is the pin-to-node precedent it loses to).

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
  "root given approval" ask itself, mitigated by the loud root confirm.

## Target matrix

Hosts from the mesh roster (<../../nebula-mesh.json>); reachability from <../../cluster/README.md>.

| Host     | Nebula IP    | Always on?   | In scope?                       | Notes                                            |
| -------- | ------------ | ------------ | ------------------------------- | ------------------------------------------------ |
| `wyrm2`  | `10.42.0.20` | yes (home)   | **yes** (`agentydragon`+`root`) | GPU box; primary always-on target                |
| `rugged` | `10.42.0.30` | no (roaming) | **yes** (`agentydragon`+`root`) | roaming tablet; fail-fast when offline; MTU 1100 |
| `iguana` | `10.42.0.31` | no (roaming) | deferred (TODO, 07-17)          | same NixOS config as `rugged`; add later         |
| `atlas`  | `10.42.0.5`  | yes (home)   | **no** (operator 07-17)         | Proxmox hypervisor; dropped from scope           |
| `pixel6` | `10.42.0.50` | —            | no                              | excluded — no host we run a service on           |

v1 creates a per-host Authentik provider (`hostexec-wyrm2`, `hostexec-rugged`) and the four groups
`hostexec-{user,root}-{wyrm2,rugged}`, all granted to the operator identity. `root` is an explicit
per-host group. `rugged`'s `root` grant has more physical exposure (roaming tablet on cellular) —
accepted (operator, 07-17); the card renders `root` loudly regardless.

## Architecture (end to end)

```text
Haku (agent, prompt-injectable)
  │  hostexec_run {host, run_as, cmd, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only; root = 2nd confirm)
  │  in-process `hostexec` tool executes in the console:
  │    · token-exchange the operator's identity → short-lived token (aud=hostexec-<host>, groups)
  │    · HTTPS POST over Nebula → https://<host>.nebula:PORT/exec   {token, run_as, argv, cwd, …}
  ▼
hostexecd on target host (systemd, root, bound to nebula1, firewalled to console peer):
  │  verify token vs Authentik JWKS: sig, aud=hostexec-<host>, exp, group hostexec-<run_as>-<host>
  │  AND token not already used (single-use replay store)
  │  → drop privileges to run_as → execve(argv) → journald/auditd log
  │  capped stdout/stderr/exit ────────────────────────────────────────────┘
  ▼
result returns through the console → ledger row RUNNING→done ; agent gets result or a promise
```

## Remaining work

- **In-process `hostexec` console MCP server** (`haku/console/tools/hostexec.py`, the
  gmail/google_calendar pattern): a `hostexec_run` tool taking the already-defined
  `HostexecRunInput` (`wire.py`). On execution it token-exchanges the operator's identity for a
  short-lived `hostexec-<host>` token (reuse `mcp_infra/authentik_auth/token_exchange.py`), POSTs
  the request to `hostexecd`, returns `BaseExecResult`. Register in
  <../../cluster/k8s/haku/console/config.yaml> **without** `server_url` (in-process). No separate
  deployment. **Console→mesh egress** is the one plumbing item: the `haku-console` pod must reach
  a Nebula IP (`10.42.0.x`) — give it mesh reachability or a thin non-MCP relay.
- **`hostexecd`** (host, **minimal Rust** — `axum` + `jsonwebtoken`): the OIDC-RS exec service.
  The exec module (`hostexecd/exec.rs` — spawn/timeout/output-caps) and the Authentik-token
  verifier (`hostexecd/authentik.rs` — RS256, `iss`/`aud=hostexec-<host>`/`exp`, and the
  `hostexec-<run_as>-<host>` group) exist and are tested. Remaining: the axum `POST /exec`
  handler; **JWKS fetch + cache** that resolves the decoding key (Authentik discovery, 30s skew);
  the single-use replay store (keyed on the token); privilege-drop to `run_as`; journald/auditd
  logging. Deployed by nix as a systemd unit on `wyrm2` + `rugged`, bound to `nebula1`,
  Nebula-firewalled, fail-closed.
- **Authentik providers + groups:** per-host `hostexec-<host>` OAuth2 providers in
  `tf/gitops/agent-machine-access` (clone `kubectl_passthrough_mcp`) with short token TTLs and a
  scope mapping emitting the `hostexec-*` group claims; the four
  `hostexec-{user,root}-{wyrm2,rugged}` groups granted to the operator; the console's token-exchange
  trust to mint per-host tokens on the operator's behalf.
- **Approval-card renderer:** `haku/console/frontend/tool_rendering/hostexec/` modeled on the
  `kubectl/` renderer — show `host`, `run_as` (`root` loud + second confirm), full `argv`, `cwd`,
  `rationale`.

## Decisions (operator, 2026-07-17)

1. **Authorization** — the operator's own **Authentik** token, verified by a fail-closed host
   service; **no bespoke standing key** (no SSH key, no console signing key). A standing-SSH-key
   variant was rejected.
2. **Transport** — a **host-side OIDC exec service (`hostexecd`) over Nebula**, not SSH and not a
   privileged pod.
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
8. **Root friction** — root renders loudly + second confirm on the card (default; adjustable).

## Architecture pivots

Kept as a record so the reasoning survives the code churn.

- **SSH-over-Nebula → host-side OIDC service (`hostexecd`).** Once auth is the Authentik token,
  the host is an OIDC resource server; SSH only added a standing transport key and
  `SSH_ORIGINAL_COMMAND` plumbing for no benefit.
- **Separate remote `hostexec-mcp` pod → in-process console tool.** The minting/exchange is custom
  code that belongs at the console (the trust boundary); a separate pod would hold the credential
  (worse) or need the console to inject it anyway (no gain).
- **Bespoke console-signed capability (argv-bound) → Authentik-native narrowing.** Built the
  capability first (Python `capability.py` + Rust `capability.rs` + cross-language JWT vectors),
  then dropped it: per-host provider + short TTL + single-use gets the token nearly as narrow
  (host/run_as/time/one-shot) using the operator's real Authentik identity and existing
  token-exchange machinery, with **no bespoke standing key**. Argv-binding's only extra is
  shrinking a leaked-but-unused token's window from "TTL" to "one command" — sub-second given
  single-use, and worthless against real compromise. Realized: `capability.rs` → `authentik.rs`
  (RS256/JWKS operator-token verifier), the Python `capability.py` retired, and the `capability`
  field dropped from `HostexecRequest`.

## Future expansion (TODO)

- **`iguana`** — add: a `hostexec-iguana` provider + `hostexec-{user,root}-iguana` groups + the
  `hostexecd` unit; identical NixOS config to `rugged`, purely additive.
- **Non-node / future hosts** — `hostexecd`-over-Nebula covers any mesh host uniformly; adding one
  is a nix unit + an Authentik provider + groups.
- **Tighten the last residual** — closing "socially engineer the operator into approving a
  malicious command via the card" is an approval-card-integrity / operator-judgment problem, not a
  transport one.

## Risks / residuals

- **The console is the trust root** (holds the operator linkage, drives token exchange) — as it
  already is for every approval-gated tool. A console compromise yields host access bounded by the
  operator's Authentik grants and revocation; there is no root-capable standing key to steal.
- **`hostexecd` is the single load-bearing component** and a root-capable network service. Keep it
  minimal, bound to `nebula1`, firewalled to the console peer, fail-closed, privilege-dropping,
  argv-`execve` (no shell); test the deny paths as security-critical.
- **Roaming hosts** (`rugged`) are frequently offline; treat unreachability as a normal,
  fast-failing outcome, not an error to retry indefinitely.
- **Fits the roadmap:** a concrete instance of <../PLAN.md> → _"letting Haku take some actions
  itself (permission-elevation tokens)"_ — an Authentik-scoped, expiring, single-use, per-host
  grant enforced by the perimeter is exactly what that section sketches.
