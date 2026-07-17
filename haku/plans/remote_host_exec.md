# Plan: remote command execution on operator machines (Haku)

**Status:** design proposal, not built; core decisions locked (operator, 2026-07-17) — see
_Decisions_ below.
**Ask:** let Haku run shell commands on the operator's machines (`wyrm2`, `rugged`, …) with
**no auto-approval**, and the ability to run as `agentydragon` **or `root`** once the operator
approves.

## TL;DR

Build a new remote MCP server `hostexec-mcp` modeled on
<../../cluster/k8s/agents/kubectl-passthrough-mcp/>, registered in the console catalog so it is
**approval-gated by construction**. On approval the console forwards the approving operator's
short-lived **Authentik** token to `hostexec-mcp`, which makes an authenticated HTTPS call
**over Nebula** to a tiny host-side service (`hostexecd`) on the target machine. `hostexecd` is
an **OIDC resource server** — the host analog of kube-apiserver in kubectl-passthrough: it
validates the Authentik token (JWKS) and a console-signed **argv countersignature**, drops
privileges to the approved `run_as`, execs, and returns the output.

**No SSH, no standing key.** Authority is purely the ephemeral forwarded token +
per-approval countersignature; the `hostexec-mcp` pod holds **no standing host credential at
all**. "May run root on `wyrm2`" is an Authentik group you grant/revoke centrally, exactly
like cluster-admin. Scope: `wyrm2` + `rugged`, `agentydragon` + `root` on both.

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
  of its own. "There is no narrower RBAC backstop underneath either path" — the approval click
  in trusted console chrome is the gate.
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

## Transport decision: a host-side OIDC exec service, not SSH

Once the host authenticates the operator's Authentik token, it **is** an OIDC resource server,
and the natural transport is a small authenticated HTTP endpoint — not sshd + a `ForceCommand`
shim.

### Recommended — `hostexecd`, a minimal host-side OIDC exec service over Nebula ✅

`hostexec-mcp` POSTs `{token, countersignature, run_as, argv, cwd, timeout_ms}` over Nebula to
`hostexecd` on the target host. `hostexecd` runs as root (systemd), validates the Authentik JWT
against cached JWKS, validates the console countersignature, drops privileges to `run_as`,
execs argv (`execve`, no shell), and returns a capped `BaseExecResult`.

- **No standing host credential anywhere.** The pod forwards only the operator's ephemeral
  token + the per-approval countersignature — the exact kubectl-passthrough property (the relay
  holds nothing standing). There is **no SSH key**, so the whole "is the key root?" surface
  disappears.
- **Natural payload + clean `run_as`.** Structured JSON, and privilege-drop happens once,
  centrally, inside `hostexecd` (root → `setuid`/`runuser` to the approved user) instead of via
  sshd login-user + `sudo` gymnastics.
- **Channel security from Nebula.** Nebula already provides encryption + peer identity; bind
  `hostexecd` to `nebula1`, firewall it to the console/pod peer, and the token requirement
  backstops reachability.

### Why not SSH (rejected)

SSH would reuse the sshd already on each host — but once auth is the Authentik token, it adds a
**transport SSH key** (an extra standing credential to manage/rotate/pin, and the entire "is
the key root?" reasoning) and `SSH_ORIGINAL_COMMAND` plumbing to smuggle the token +
countersignature + argv, for **no benefit**. The honest counterpoint — "sshd is battle-tested,
`hostexecd` is new root-capable code" — loses because we are writing the root-capable verifier
either way; sshd would only wrap it and bolt on a key.

### Why not a privileged pod-on-node (rejected)

All in-scope hosts are k8s nodes, so a privileged `hostPID` pod could `nsenter` the host
(<../../cluster/k8s/egress-proxy-rugged/> is the pin-to-node precedent). Rejected: it needs a
**standing privileged/`hostPID` root pod resident on each node** — including the roaming
personal tablet `rugged` — a larger always-on footprint than a minimal firewalled service,
likely blocked by pod-security/Kyverno, with clunky `run_as` and a weaker audit trail; and it
covers only k8s nodes. `hostexecd` over Nebula covers any mesh host uniformly.

## Authorization model (no standing key, no theater)

State it plainly. `hostexecd` executes **only** when both independent checks pass, and they
must agree on `(host, run_as)`:

1. **Authentik token** (JWKS-validated: sig/aud/exp) whose `hostexec-<run_as>-<host>` group
   claim authorizes _this operator identity_ to run as `run_as` on this host. This is the
   revocable authority — a group you grant/revoke centrally in `tf/gitops/agent-machine-access`.
2. **Console argv countersignature** over `(host, run_as, sha256(argv), cwd, nonce, exp)`,
   signed at approval time with a key that lives with the console (the trust boundary, a Secret
   Haku cannot read); public key deployed to each host. `nonce` single-use + short `exp` make
   it bind _this exact command, once_.

What this genuinely buys (not theater):

- **No credential the pod holds "is root."** The pod holds no standing host credential; token
  and countersignature are both ephemeral and per-approval. Key-exfiltration-grants-root — the
  worry that dominated the SSH variant — is structurally absent.
- **Time-boxed, attributed, centrally revocable.** Tokens expire; every exec is tied to the
  Authentik identity and logged; revoke the group in Authentik → every host refuses, touching
  no host config.
- **Exact-command binding.** A compromised relay can't swap or replay commands (the
  countersignature plus its single-use nonce bind the exact argv).

The one load-bearing component and the irreducible residual:

- **`hostexecd`'s fail-closed validation is the single load-bearing component.** If it is
  sloppy (accepts a missing/expired token, skips the countersignature, doesn't drop privileges
  correctly), the whole model collapses. Treat it as security-critical reviewed code; test the
  deny paths.
- **Residual:** a **live, fully-compromised `hostexec-mcp` pod** holds a live forwarded token +
  countersignature in-flight, so within that window it can run _the exact approved command_. It
  cannot swap or replay. The irreducible hop no relay design removes: a compromised relay
  socially engineering the operator into approving a malicious command via the card — which is
  the "root given approval" ask itself, mitigated by the loud root confirm.

Bottom line: you cannot have "agent runs root given approval" without _some_ component, if
compromised at the instant of a live approval, being able to run the approved command. The job
is to guarantee root never happens _without_ an approval, only for the exact command approved,
always attributed, time-boxed, and revocable. This design keeps all of that.

## Target matrix

Hosts from the mesh roster (<../../nebula-mesh.json>); reachability from
<../../cluster/README.md>.

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
  │  hostexec_run {host, run_as, argv, cwd?, timeout_ms?}  (approval envelope + rationale)
  ▼
haku-console  /mcp  ── submit_and_wait ──> McpToolCall row (PENDING_APPROVAL)
  │  operator clicks Approve in trusted chrome (CSRF, Authentik-operator-only; root = 2nd confirm)
  │  console forwards approving operator's short-lived Authentik token (aud=hostexec)
  │  console countersigns (host, run_as, sha256(argv), cwd, nonce, exp)
  ▼
hostexec-mcp  (own namespace; NO standing host credential — forwards token + countersig only)
  │  HTTPS POST over Nebula → https://<host>.nebula:PORT/exec
  ▼
hostexecd on target host (systemd, root, bound to nebula1, firewalled to console/pod peer):
  │  validate Authentik JWT (JWKS: sig/aud/exp) + group authorizes run_as
  │  AND validate console argv countersignature (agree on host/run_as; nonce fresh, unexpired)
  │  → drop privileges to run_as → execve(argv) → journald/auditd log
  │  capped stdout/stderr/exit ────────────────────────────────────────────┘
  ▼
result returns through console → ledger row RUNNING→done ; agent gets result or a promise
```

## Components to build (v1)

- **`hostexec-mcp`** (cluster): subclass `EnhancedFastMCP` (model: <../../mcp_infra/exec/direct.py>).
  One `hostexec_run` tool taking `host`, `run_as`, and the exec fields from
  <../../mcp_infra/exec/models.py> (`ExecArgsBase` / `BaseExecResult`, byte/timeout caps). It
  resolves the forwarded operator token, requests the console countersignature, POSTs to
  `hostexecd`, returns `BaseExecResult`. Bazel `py_binary → oci_image` (template:
  `grocy_mcp/BUILD.bazel`); k8s Deployment/Service/HTTPRoute/flux-kustomization under
  `cluster/k8s/agents/hostexec-mcp/` (own namespace, unprivileged); reach the mesh (give the pod
  a Nebula identity or route via node — confirm during build, same requirement SSH would have
  had). Register in <../../cluster/k8s/haku/console/config.yaml> with `operator_oauth`.
- **`hostexecd`** (host): a **minimal** OIDC-RS exec service. Validates Authentik JWT + console
  countersignature, drops privileges to `run_as`, execs argv (reuse the `run_proc` pattern in
  <../../mcp_infra/exec/subprocess.py>), caps output, logs to journald/auditd. Deployed by nix
  as a systemd unit on `wyrm2` + `rugged` (uniform `nix/nixos/hosts/<host>/default.nix`), bound
  to `nebula1`, Nebula-firewalled to the console/pod peer, fail-closed. **Open decision:**
  language/shape (minimal Rust vs. Python reusing the shared capability lib + JWT tooling).
- **`capability` (shared lib):** the countersignature model + sign (console) / verify
  (`hostexecd`). Ed25519 over `(host, run_as, sha256(argv), cwd, nonce, exp)`. Fully unit-tested,
  transport-independent — buildable first.
- **Authentik provider + groups:** a `hostexec` OAuth2 application/provider in
  `tf/gitops/agent-machine-access` (clone `kubectl_passthrough_mcp`) with a scope mapping
  emitting `hostexec-*` group claims; the four `hostexec-{user,root}-{wyrm2,rugged}` groups
  granted to the operator identity.
- **Console argv-signing + approval hook:** a signing keypair (private key a `haku-console`
  Secret Haku can't read, minted by `tf/gitops/agent-machine-access`; public key to each host
  via nix); the console mints the countersignature at approval time for `hostexec-mcp` calls.
- **Approval-card renderer:** `haku/console/frontend/tool_rendering/hostexec/` modeled on the
  `kubectl/` renderer — show `host`, `run_as` (`root` loud + second confirm), full `argv`,
  `cwd`, `rationale`.
- **Audit (double-entry):** console ledger `McpToolCall` + host-side journald/auditd.

## Decisions (operator, 2026-07-17)

1. **Authorization** — Authentik-minted operator tokens + a fail-closed host verifier; **no
   standing key**. L0 (standing SSH key) rejected.
2. **Transport** — a **host-side OIDC exec service (`hostexecd`) over Nebula**, not SSH and not
   a privileged pod. SSH is redundant once auth is the Authentik token and would only add a
   standing key.
3. **Command binding** — **include full argv**: console countersigns the exact `argv`
   (+`host`/`run_as`/`nonce`/`exp`); a compromised relay can't swap or replay.
4. **Root scope** — `root` on **both `wyrm2` and `rugged`** (four
   `hostexec-{user,root}-{wyrm2,rugged}` groups). `rugged`'s exposure accepted.
5. **`iguana`** — deferred (TODO below).
6. **Root friction** — root renders loudly + second confirm on the card (default; adjustable).

## Open decisions

- **`hostexecd` language/shape** — minimal Rust (small, no interpreter on the host, easy to
  audit) vs. Python (reuses the shared `capability` lib + the repo's Authentik JWT tooling).

## Future expansion (TODO)

- **`iguana`** — add: `hostexec-{user,root}-iguana` groups + the `hostexecd` unit; identical
  NixOS config to `rugged`, purely additive.
- **Non-node / future hosts** — `hostexecd`-over-Nebula covers any mesh host uniformly (this is
  why the host-service transport beat the pod-on-node path); adding one is a nix unit + groups.
- **Tighten the last residual** — closing "a compromised relay gets a malicious command
  approved via the card" is an approval-card-integrity / operator-judgment problem, not a
  transport one.

## Risks / residuals

- **Live-relay residual (narrowed, not zero):** a compromised `hostexec-mcp` pod within a live
  token window can run _the exact approved command_ — it cannot swap or replay (countersignature
  - nonce). Irreducible hop: social-engineering an approval, mitigated by the loud root confirm.
- **`hostexecd` is the single load-bearing component** and a root-capable network service.
  Keep it minimal, bound to `nebula1`, firewalled to the console/pod peer, fail-closed,
  privilege-dropping, argv-`execve` (no shell); test the deny paths as security-critical.
- **Console + Authentik are the trust roots** — as they already are for every approval-gated
  tool and all machine access. No new trust root (the argv-signing key lives with the console,
  already the trust boundary).
- **Roaming hosts** (`rugged`) are frequently offline; treat unreachability as a normal,
  fast-failing outcome, not an error to retry indefinitely.
- **Fits the roadmap:** a concrete instance of PLAN.md → _"letting Haku take some actions
  itself (permission-elevation tokens)"_ — an Authentik-scoped, expiring, per-approval,
  argv-bound grant enforced by the perimeter is exactly what that section sketches.
  </content>
