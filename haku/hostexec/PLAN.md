# hostexec — remote command execution on operator machines

**Ask (operator, 2026-07-17):** let Haku run shell commands on the operator's machines
(`wyrm2`, `rugged`, …) with **no auto-approval**, and the ability to run as `agentydragon`
**or `root`** once the operator approves. Core decisions are locked (see _Decisions_); the
capability/wire contract exists (`capability.py`, `wire.py`), the rest is remaining work below.

## TL;DR

A new `hostexec` **in-process** MCP server in the console (the gmail/google_calendar/haku_routine
pattern — `haku/console/tools/*.py`), registered in the console catalog so it is
**approval-gated by construction**. On approval the console mints a console-signed capability JWT,
obtains the approving operator's short-lived **Authentik** token, and makes an authenticated HTTPS
call **over Nebula** to a tiny host-side service (`hostexecd`) on the target machine. `hostexecd`
is an **OIDC resource server** — the host analog of kube-apiserver in kubectl-passthrough: it
validates the Authentik token (JWKS) and the console capability JWT, drops privileges to the
approved `run_as`, execs, and returns the output.

**Why in-process, not a separate `hostexec-mcp` pod.** The capability signing key + minting is
custom code that must live in the console (the trust boundary — it holds secrets Haku can't read
and is where approval happens). A separate remote exec server would either hold that signing key
(a worse place for it) or need the console to inject the capability into every call anyway — so it
buys nothing over an in-process tool. The only thing a mesh-attached pod uniquely provides is a
network egress point to Nebula; that is a plumbing detail (give the console mesh reachability, or a
thin non-MCP relay), not a reason for a whole MCP server. See _Decisions_.

**No single standing skeleton key.** `hostexecd` requires **both** the operator's live Authentik
token (independently verified, revocable) **and** the console capability JWT — so the console
signing key alone (at rest) does not grant host access, and revoking the Authentik group cuts
access centrally. "May run root on `wyrm2`" is an Authentik group you grant/revoke like
cluster-admin. Scope: `wyrm2` + `rugged`, `agentydragon` + `root` on both.

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
  <../../cluster/k8s/agents/kubectl-passthrough-mcp/app/configmap.yaml>); it holds no credential of
  its own. "There is no narrower RBAC backstop underneath either path" — the approval click in
  trusted console chrome is the gate.
- **Everything is in the ledger** (`McpToolCall` row, scoped to the exact principal —
  `haku/console/tool_call_service.py`), and the **approval surface is trusted chrome only**
  (security.md invariant #4).

Two consequences make most of the work free:

1. **"No auto-approval" costs zero code.** Any server in the console catalog is approval-gated
   by default; a tool auto-approves only if added to `UNCONDITIONAL_AUTO_APPROVE`
   (`haku/console/auto_approval.py`). We never add the exec tool there — comment it as such at
   the definition. Non-agent callers never auto-approve regardless.
2. **The approval lifecycle, audit ledger, promise/deep-link semantics, and operator-identity
   model all already exist.** We add a tool server + a host service, not an approval system.

So the real design question is narrow: **what carries an approved command to a shell on
`wyrm2`/`rugged`, and under what credential.**

## Transport: a host-side OIDC exec service, not SSH

Once the host authenticates the operator's Authentik token, it **is** an OIDC resource server,
and the natural transport is a small authenticated HTTP endpoint — not sshd + a `ForceCommand`
shim.

The in-process console tool POSTs `{token, capability, run_as, argv, cwd, timeout_ms}` over
Nebula to `hostexecd` on the target host. `hostexecd` runs as root (systemd), validates the
Authentik JWT against cached JWKS, validates the console capability JWT, drops privileges to
`run_as`, execs argv (`execve`, no shell), and returns a capped `BaseExecResult`.

- **No SSH key, no host-resident credential.** The capability signing key lives at the console
  (the trust boundary), not on any host and not in a separate exec pod; hosts hold only the
  public key. The whole "is the SSH key root?" surface disappears.
- **Natural payload + clean `run_as`.** Structured JSON, and privilege-drop happens once,
  centrally, inside `hostexecd` (root → `setuid`/`runuser` to the approved user) instead of via
  sshd login-user + `sudo` gymnastics.
- **Channel security from Nebula.** Nebula already provides encryption + peer identity; bind
  `hostexecd` to `nebula1`, firewall it to the console's mesh peer, and the token requirement
  backstops reachability.

Rejected alternatives: **SSH** — once auth is the Authentik token, it only adds a standing
transport key and `SSH_ORIGINAL_COMMAND` plumbing, for no benefit; we write the root-capable
verifier either way. **Privileged pod-on-node** — a standing `hostPID` root pod resident on
each node (including the roaming tablet `rugged`), likely blocked by pod-security/Kyverno, with
clunky `run_as`; covers only k8s nodes, whereas `hostexecd`-over-Nebula covers any mesh host
(<../../cluster/k8s/egress-proxy-rugged/> is the pin-to-node precedent it loses to).

## Authorization model (no standing key, no theater)

`hostexecd` executes **only** when both independent checks pass, and they must agree on
`(host, run_as)`:

1. **Authentik token** (JWKS-validated: sig/aud/exp) whose `hostexec-<run_as>-<host>` group
   claim authorizes _this operator identity_ to run as `run_as` on this host. The revocable
   authority — a group you grant/revoke centrally in `tf/gitops/agent-machine-access`.
2. **Console-minted capability JWT** — an EdDSA (Ed25519) JWT carrying `host`, `run_as`, `argv`,
   `cwd`, `nonce`, `exp` and `aud=hostexec-capability`, minted at approval time with a key that
   lives with the console (the trust boundary, a Secret Haku cannot read); public key deployed to
   each host. `hostexecd` verifies the signature/aud/exp and re-checks the request's
   `host`/`run_as`/`argv` equal the signed claims; `nonce` single-use + short `exp` bind _this
   exact command, once_. Standard JWT (PyJWT signer + Rust `jsonwebtoken` verifier) — no custom
   cross-language encoding. Signer + verifier + pinned vectors: `capability.py`,
   `hostexecd/capability.rs`.

What this genuinely buys (not theater):

- **The console signing key alone is not a skeleton key.** `hostexecd` requires a live operator
  Authentik token bearing the group _in addition to_ the capability, so exfiltrating the signing
  key at rest does not grant host access. Key-exfiltration-grants-root — the worry that dominated
  the SSH variant — is structurally absent.
- **Time-boxed, attributed, centrally revocable.** Tokens expire; every exec is tied to the
  Authentik identity and logged; revoke the group in Authentik → every host refuses, touching
  no host config.
- **Exact-command binding.** The capability JWT (and its single-use `nonce`) bind the exact
  argv, so `hostexecd` runs precisely the command that was approved and signed — no ambiguity
  between "approved" and "executed".

The one load-bearing component and the irreducible residual:

- **`hostexecd`'s fail-closed validation is the single load-bearing component.** If it is
  sloppy (accepts a missing/expired token, skips the capability, doesn't drop privileges
  correctly), the whole model collapses. Treat it as security-critical reviewed code; test the
  deny paths.
- **The console is the trust root** (it mints capabilities and obtains operator tokens
  in-process). A console compromise yields host access — but the console is already the approval
  authority and secret store, so this is the same trust root every approval-gated tool has, not a
  new one; concentrating the signing key there (vs. a less-trusted exec pod) is the safer choice.
  The irreducible hop no design removes: socially engineering the operator into approving a
  malicious command via the card — which is the "root given approval" ask itself, mitigated by
  the loud root confirm.

Bottom line: you cannot have "agent runs root given approval" without _some_ component, if
compromised at the instant of a live approval, being able to run the approved command. The job
is to guarantee root never happens _without_ an approval, only for the exact command approved,
always attributed, time-boxed, and revocable. This design keeps all of that.

## Target matrix

Hosts from the mesh roster (<../../nebula-mesh.json>); reachability from <../../cluster/README.md>.

| Host     | Nebula IP    | Always on?   | In scope?                       | Notes                                            |
| -------- | ------------ | ------------ | ------------------------------- | ------------------------------------------------ |
| `wyrm2`  | `10.42.0.20` | yes (home)   | **yes** (`agentydragon`+`root`) | GPU box; primary always-on target                |
| `rugged` | `10.42.0.30` | no (roaming) | **yes** (`agentydragon`+`root`) | roaming tablet; fail-fast when offline; MTU 1100 |
| `iguana` | `10.42.0.31` | no (roaming) | deferred (TODO, 07-17)          | same NixOS config as `rugged`; add later         |
| `atlas`  | `10.42.0.5`  | yes (home)   | **no** (operator 07-17)         | Proxmox hypervisor; dropped from scope           |
| `pixel6` | `10.42.0.50` | —            | no                              | excluded — no host we run a service on           |

v1 creates four Authentik groups `hostexec-{user,root}-{wyrm2,rugged}`, all granted to the
operator identity. `root` is an explicit per-host group, not implicit. `rugged`'s `root` grant
has more physical exposure (roaming personal tablet on cellular) — accepted (operator, 07-17);
the card renders `root` loudly regardless.

## Architecture (end to end)

```text
Haku (agent, prompt-injectable)
  │  hostexec_run {host, run_as, cmd, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only; root = 2nd confirm)
  │  in-process `hostexec` tool executes in the console:
  │    · mints capability JWT (EdDSA: host, run_as, argv, cwd, nonce, exp; aud=hostexec-capability)
  │    · obtains approving operator's short-lived Authentik token (aud=hostexec)
  │    · HTTPS POST over Nebula → https://<host>.nebula:PORT/exec   {token, capability, argv, …}
  ▼
hostexecd on target host (systemd, root, bound to nebula1, firewalled to console peer):
  │  validate Authentik JWT (JWKS: sig/aud/exp) + group authorizes run_as
  │  AND validate console capability JWT (agree on host/run_as/argv; nonce fresh, unexpired)
  │  → drop privileges to run_as → execve(argv) → journald/auditd log
  │  capped stdout/stderr/exit ────────────────────────────────────────────┘
  ▼
result returns through the console → ledger row RUNNING→done ; agent gets result or a promise
```

## Remaining work

- **In-process `hostexec` console MCP server** (`haku/console/tools/hostexec.py`, the
  gmail/google_calendar pattern): a `hostexec_run` tool taking the already-defined
  `HostexecRunInput` (`wire.py`). On execution it mints the capability JWT (via `capability.py`
  with the console signing key), obtains the approving operator's `hostexec`-audience Authentik
  token (token exchange — reuse `mcp_infra/authentik_auth/token_exchange.py`, the grocy pattern),
  POSTs `HostexecRequest` to `hostexecd`, and returns `BaseExecResult`. Register in
  <../../cluster/k8s/haku/console/config.yaml> **without** `server_url` (in-process). No separate
  deployment/oci_image. **Console→mesh egress** is the one plumbing item: the `haku-console` pod
  must reach a Nebula IP (`10.42.0.x`) — give it mesh reachability or a thin non-MCP relay;
  resolve during build.
- **`hostexecd`** (host, **minimal Rust** — `axum` + `jsonwebtoken`): the OIDC-RS exec service.
  The capability verifier (`hostexecd/capability.rs`) and the exec module (`hostexecd/exec.rs` —
  spawn/timeout/output-caps) exist; remaining is the axum `POST /exec` handler, Authentik JWT
  validation against cached JWKS (incl. the `hostexec-<run_as>-<host>` group check), the nonce
  replay store, privilege-drop to `run_as`, and journald/auditd logging. Deployed by nix as a
  systemd unit on `wyrm2` + `rugged` (uniform `nix/nixos/hosts/<host>/default.nix`), bound to
  `nebula1`, Nebula-firewalled to the console peer, fail-closed.
- **Authentik provider + groups:** a `hostexec` OAuth2 application/provider in
  `tf/gitops/agent-machine-access` (clone `kubectl_passthrough_mcp`) with a scope mapping
  emitting `hostexec-*` group claims; the four `hostexec-{user,root}-{wyrm2,rugged}` groups
  granted to the operator identity.
- **Console capability-signing key:** an Ed25519 signing keypair (private key a `haku-console`
  Secret Haku can't read, minted by `tf/gitops/agent-machine-access`; public key PEM to each host
  via nix). The in-process `hostexec` tool signs with it directly — no cross-pod injection.
- **Approval-card renderer:** `haku/console/frontend/tool_rendering/hostexec/` modeled on the
  `kubectl/` renderer — show `host`, `run_as` (`root` loud + second confirm), full `argv`,
  `cwd`, `rationale`.

## Decisions (operator, 2026-07-17)

1. **Authorization** — Authentik-minted operator tokens + a fail-closed host verifier; **no
   standing key**. A standing-SSH-key variant was rejected.
2. **Transport** — a **host-side OIDC exec service (`hostexecd`) over Nebula**, not SSH and not
   a privileged pod. SSH is redundant once auth is the Authentik token and would only add a
   standing key.
3. **`hostexecd` language** — minimal **Rust** (smallest root TCB, no interpreter on the host).
4. **Command binding** — **full argv**: the console capability JWT carries the exact `argv`
   (+`host`/`run_as`/`nonce`/`exp`), so "approved" and "executed" can't diverge.
5. **In-process, not a separate MCP server** — the `hostexec` tool runs in-process in the console
   (gmail/google_calendar pattern), not as a separate `hostexec-mcp` deployment. The capability
   signing key + minting is custom code that belongs at the console (the trust boundary); a
   separate pod would hold that key (worse) or need the console to inject the capability anyway
   (no gain). Console→mesh egress is the only thing to plumb.
6. **Root scope** — `root` on **both `wyrm2` and `rugged`**. `rugged`'s exposure accepted.
7. **`iguana`** — deferred (TODO below).
8. **Root friction** — root renders loudly + second confirm on the card (default; adjustable).

## Future expansion (TODO)

- **`iguana`** — add: `hostexec-{user,root}-iguana` groups + the `hostexecd` unit; identical
  NixOS config to `rugged`, purely additive.
- **Non-node / future hosts** — `hostexecd`-over-Nebula covers any mesh host uniformly (this is
  why the host-service transport beat the pod-on-node path); adding one is a nix unit + groups.
- **Tighten the last residual** — closing "socially engineer the operator into approving a
  malicious command via the card" is an approval-card-integrity / operator-judgment problem, not
  a transport one.

## Risks / residuals

- **The console is the trust root** (mints capabilities, obtains operator tokens in-process) — as
  it already is for every approval-gated tool and all machine access. A console compromise yields
  host access, but that is the same trust root, not a new one; the signing key lives at the
  console rather than a less-trusted exec pod deliberately. Irreducible hop: social-engineering an
  approval, mitigated by the loud root confirm.
- **`hostexecd` is the single load-bearing component** and a root-capable network service.
  Keep it minimal, bound to `nebula1`, firewalled to the console's mesh peer, fail-closed,
  privilege-dropping, argv-`execve` (no shell); test the deny paths as security-critical.
- **Roaming hosts** (`rugged`) are frequently offline; treat unreachability as a normal,
  fast-failing outcome, not an error to retry indefinitely.
- **Fits the roadmap:** a concrete instance of <../PLAN.md> → _"letting Haku take some
  actions itself (permission-elevation tokens)"_ — an Authentik-scoped, expiring, per-approval,
  argv-bound grant enforced by the perimeter is exactly what that section sketches.
