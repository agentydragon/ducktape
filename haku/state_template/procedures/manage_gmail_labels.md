# Manage Gmail labels (your one sanctioned world-write)

Your base manual grants exactly one change you may make to the world outside `haku-state`:
organizing the operator's Gmail with labels under `haku/`, via the `gmail-labeling` MCP
server. This file is **your policy** for that capability — how, when, and which — and it is
yours to evolve. Base only grants the capability and names its bound (base → _Hard rules_);
the **boundary is enforced by the server**, not by this file, so you cannot widen it by
editing here: every operation is confined to `haku/` labels by construction (nothing outside
the prefix, never message content, rejected server-side before any Gmail call).

This is the one place the usual **propose-only** rule flips to **do**: for `haku/` labels you
act directly; for everything else in Gmail (archiving, filters, replies, non-`haku/` labels)
you still only surface and frame, and an executor with write access acts (see
`triage_and_delegation.md`).

## How to reach it

It's a bearer-gated MCP server — the shared transport how-to (fastmcp, with a curl fallback)
is in your base guide `sources/mcp_over_http.md`. Only the URL, secret, and tools differ:

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-gmail-labeling-token \
  -o jsonpath='{.data.token}' | base64 -d)
URL=https://gmail-labeling.allegedly.works/mcp

fastmcp list "$URL" --auth "$TOKEN" --transport http --input-schema   # tools + schemas
```

Reference `"$TOKEN"`, never the literal value, so the bearer stays out of your transcript.

| Tool            | Effect                                                                  |
| --------------- | ----------------------------------------------------------------------- |
| `list_labels`   | List managed labels (those under `haku/`).                              |
| `modify_labels` | Add and/or remove managed labels across a batch of threads in one call. |
| `create_label`  | Create a managed label without applying it.                             |
| `rename_label`  | Rename a managed label (both names must be under `haku/`).              |
| `delete_label`  | Delete a managed label (drops it from every thread).                    |

`modify_labels` takes a list of `thread_ids` plus `add`/`remove` label lists (Gmail's
own `batchModify` shape) — label many threads at once instead of one call per thread.

## Current policy

**The capability is live, but no autonomous labeling scheme is defined yet.** Until the
operator and you have agreed one, stay conservative: use `list_labels` to see the current
`haku/` taxonomy, and only apply/remove labels in service of an explicit, operator-visible
purpose you can name in an item — not a speculative scheme. When you have an idea for a
labeling system worth running (e.g. a `haku/triage/*` or `haku/waiting-on` scheme), **propose
it as an item first**, with the exact labels and the rule for applying them, and let the
operator approve before you run it broadly. A small reversible trial on a few threads is fine;
a sweeping relabel is a proposal.

## Knobs (evolve these as the scheme firms up)

This is the scaffold for the dials you'll add — record the operator's choices here as they're
made, so the policy is explicit and auditable:

- **Enabled / paused** — a master switch you honor (default: conservative, as above).
- **Sub-namespaces** — which `haku/...` sub-prefixes are in play and what each means.
- **Triggers** — what conditions make you apply or remove a given label (the labeling rules).
- **Volume guardrails** — a self-imposed cap on threads touched per run, so a bug or bad rule
  can't relabel the whole mailbox before the operator notices.
- **Preview vs. apply** — when to dry-run (compute the label changes, surface them as an item
  for approval) vs. apply directly.
- **Reconciliation** — whether/when to re-derive labels from rules and prune drift.

## Discipline

- **Log every labeling action** in the day's run log (`log/YYYY-MM-DD.md`): which threads,
  which labels, why. This is your audit trail for a capability that changes the operator's
  mailbox.
- **Reversibility first** — prefer schemes you can cleanly undo (`modify_labels` removal /
  `delete_label` stay within the same bound). Confirm a rule on a handful of threads before
  any breadth.
- **Surface, don't hide** — when you run a labeling pass, put a short summary on the dashboard
  so the operator always sees what you changed.
