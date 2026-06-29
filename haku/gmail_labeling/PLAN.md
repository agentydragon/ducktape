# haku/gmail_labeling — namespaced Gmail label management over MCP

**Status:** the server is implemented in this directory. The closure invariant is now
the contract in `SPEC.md`; run/setup detail is in `README.md`. This doc keeps the design
rationale (why no Airlock, the credential model) and the remaining **deployment** open
questions. Trim or tombstone it once the server is deployed.

## What it is

A small, first-class MCP server that talks to the Gmail REST API directly and exposes
**only** label management, hard-scoped to a configurable namespace (`allowed_prefix`,
e.g. `haku/`). It lets Haku autonomously create, apply, remove, and tidy labels **within
its own namespace** without touching anything else in the mailbox.

It is **not** a facade over another MCP server (cf. the Tana/Grocy read-only facades,
which wrap an upstream MCP and filter its tool surface). It holds its own `gmail.modify`
credential and calls Gmail directly — closer in kind to `grocy_mcp/` or to
`gmail_archiver/` exposed over MCP.

## Why no Airlock

The repo's own preference order (`haku/PLAN.md`) is: **(1) scope the credential,
(2) filter the tool surface, (3) human-in-the-loop.** Airlock is rung 3 — the fallback
for capabilities that _can't_ be made safe by construction. This server is rung 2: its
tool surface is narrow enough that every possible call is safe, so there is no decision
left for a human (or an Airlock predicate) to make. Putting Airlock in front would be an
always-`Approved` no-op.

This also honors Haku's trust-boundary doctrine: the boundary is enforced **outside the
agent**, at the credential/perimeter level — here, by the server's tool surface and its
scoped credential — **never** by Haku's instructions. A confused or prompt-injected Haku
still cannot exceed the namespace, because it physically lacks any tool or credential to
do so.

## The closure invariant (the contract)

> Every operation is closed over `allowed_prefix`. The set of labels the server can
> read, create, mutate, or delete is exactly the labels whose display name starts with
> `allowed_prefix` — before **and** after every call. No operation may move a label
> across that boundary in either direction.

Consequences:

- **Constraint is on the label, never the thread.** Applying a private, namespaced label
  to _any_ thread is non-destructive and reversible, so threads are unconstrained; labels
  are hard-bounded to the prefix.
- **System labels are excluded for free.** Every mutating tool takes a label **name** (not
  a raw Gmail `labelId`), resolves it to a user label under `allowed_prefix`, and rejects
  anything else. System display names (`INBOX`, `TRASH`, `SPAM`, `IMPORTANT`, `UNREAD`, …)
  don't start with `haku/`, so the destructive label-membership surface (archive, trash,
  spam, read-state) is unreachable without special-casing.
- **Name-at-the-boundary, not ID.** The upstream Gmail tools take opaque `labelIds`, which
  makes a name-prefix policy awkward to enforce externally. Taking names here and resolving
  name→id internally is what lets the prefix rule be enforced structurally.

## Tool set

All mutating tools enforce: every label name argument must start with `allowed_prefix`,
else the call is rejected (raised, not silently dropped).

| Tool           | Args                | Behavior                                                                     | Prefix constraint                                                                             |
| -------------- | ------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `list_labels`  | —                   | List labels under `allowed_prefix` (id, name, message/thread counts).        | Returns only in-namespace labels; not a general Gmail reader.                                 |
| `apply_label`  | `thread_id`, `name` | Add label to thread; auto-creates the label if missing. Idempotent.          | `name` under prefix. Thread unconstrained.                                                    |
| `remove_label` | `thread_id`, `name` | Remove label from thread.                                                    | `name` under prefix. Thread unconstrained.                                                    |
| `create_label` | `name`              | Explicit creation (for when you don't want `apply_label`'s implicit create). | `name` under prefix.                                                                          |
| `rename_label` | `old`, `new`        | Rename within the namespace.                                                 | **Both** `old` and `new` under prefix.                                                        |
| `delete_label` | `name`              | Delete the label (drops it from every thread at once).                       | `name` under prefix. Sharpest in-namespace tool; blast radius still bounded to the namespace. |

### Why `rename_label` checks both sides

One-sided checks escape the namespace:

- destination-only → rename an existing user label `Important` into `haku/whatever`,
  hijacking a label that already carries years of mail into the agent's namespace.
- source-only → rename `haku/triaged` → `Inbox-Critical`, moving a label (and all its
  tagged mail) _out_ into the user's general label space.

Requiring both sides keeps the closure intact: rename is pure in-namespace housekeeping
(`haku/triage` → `haku/triaged`).

Granularity: tools operate at **thread** level (the natural unit for triage). Message-level
variants can be added later if a need appears.

## Auth & the two credential layers

Two separate credentials, so that reaching the agent-facing one never yields the
mailbox-write one:

1. **Haku → labeling server (agent-facing bearer).** The MCP endpoint is bearer-gated,
   modeled on the `tana-mcp-ro` facade: a `static_bearer` Haku reads from a k8s secret
   reflected into `haku-sandbox` (e.g. `haku-gmail-labeling-token`), upgradeable to an
   Authentik OIDC (`mcp_oauth`) mode later — same trajectory noted for `tana-mcp-ro`.
   This bearer authenticates Haku to the server; it grants **only** the namespaced
   labeling tool surface, nothing more.
2. **Labeling server → Gmail (`gmail.modify` OAuth).** **Provisioned and rotated by
   Airlock**, which already manages Google OAuth (it fronts `google-workspace-mcp` and
   reflects `google-client-credentials`). The server owns no OAuth client of its own — it
   reads the resulting token from a secret in its own namespace (_not_ `haku-sandbox`).
   Haku has no RBAC to read it, and its own `google-access-token` stays `.readonly`, so
   even a fully compromised Haku cannot obtain mailbox-write scope; it can only ask the
   server to do namespaced labeling.

## Placement & trust boundary

Code lives here, as a Haku subcomponent (`haku/gmail_labeling/`). The deployment question
is what the **confidentiality boundary** should be, and the console is the reference for
it: `haku/console` runs in its own `haku-console` namespace (deliberately not
`haku-sandbox`), holds secrets Haku cannot read, sits outside the `haku-mitmproxy` egress
fence, and acts on the world through a tiny PR-gated allowlist in reviewed code. The
`gmail.modify` credential needs exactly that isolation.

Two ways to get it, with a real distinction:

- **Sibling MCP service (lean), modeled on `tana-mcp-ro`.** Own namespace + own secret +
  bearer-gated agent access. This is the established shape for "Haku calls an MCP server
  holding a credential it shouldn't have," and crucially it matches the **access pattern**:
  agent-triggered, bearer-authenticated.
- **Fold into `haku/console`.** Reuses the console's existing confidentiality namespace,
  secret-handling, and audit logging — but the console's write surface today is
  **operator-triggered** (Authentik operator-only + CSRF double-submit), built around a
  human behind the SPA. The labeling capability is **agent-triggered** (autonomous, no
  operator in the loop). Hosting it in the console means adding a second, contradictory
  auth model to one service and muddying the "what's the worst case if agent-authored UI
  fired this" analysis the console's two-tier split rests on.

Lean: sibling service. It replicates the console's _boundary_ (own namespace, secret Haku
can't read, reviewed enforcement) while keeping the console's _operator-only_ auth story
clean — and it reuses the `tana-mcp-ro` bearer pattern Haku already speaks.

## Doctrine flag

This is **Haku's first autonomous write to the world.** Today `haku/PLAN.md` and
`haku/base/instructions.md` state Haku "never acts on the world itself; it finds, frames,
recommends, and the operator hands off." Namespaced labels are the safest possible place to
break that seal — private to the account, reversible, low blast radius — but it is a real
change to Haku's contract and must land in Haku's docs/`SPEC.md`, not just here.

## Resolved

- **`delete_label` and `rename_label` ship in v0** — both bounded to the namespace
  (`rename` requires both names under the prefix). `delete` is the sharpest in-namespace op.
- **`gmail.modify` token is Airlock-provisioned/rotated** (see _Auth_ above), not a
  dedicated OAuth client this server owns.

## Deployment (landed)

Built as a **sibling MCP service** (the `tana-mcp-ro` shape), not in the console:

- Image `ghcr.io/agentydragon/gmail-labeling` (`//haku/gmail_labeling:image`), pushed by
  `push-images.yml`, tag-tracked by Flux image automation.
- Manifests in `cluster/k8s/agents/gmail-labeling/` (own namespace, bearer SOPS secret
  reflected into `haku-sandbox`, public bearer-gated HTTPRoute).
- `gmail.modify` token provisioned/rotated by a new Airlock `gmail_modify` OAuth provider;
  the access-only token is ESO-mirrored into the `gmail-labeling` namespace (never a
  sandbox). Deployment + one-time OAuth bootstrap: `cluster/k8s/agents/gmail-labeling/README.md`.

Remaining: register the server in Haku's own MCP config (so Haku actually calls it), and the
one-time `gmail.modify` OAuth consent at `airlock.allegedly.works/oauth/authorize/gmail_modify`.
