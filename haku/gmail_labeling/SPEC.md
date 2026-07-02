# haku/gmail_labeling — SPEC

What this server guarantees to an operator who attaches it to an agent.

## Closure invariant

> Every operation is closed over `allowed_prefix`: the set of labels the server
> can read, create, mutate, or delete is exactly the labels whose display name
> starts with `allowed_prefix` — before **and** after every call. No operation
> moves a label across that boundary in either direction.

Consequences the operator can rely on:

- **The constraint is on the label, never the thread.** Any thread may gain or lose
  a managed label; no thread state outside managed labels is reachable.
- **System labels are unreachable.** `INBOX`, `TRASH`, `SPAM`, `IMPORTANT`,
  `UNREAD`, … never start with the prefix, so the destructive archive / trash /
  spam / read-state surface cannot be invoked.
- **`rename_label` requires both names under the prefix** — a label can be renamed
  within the namespace but never moved into or out of it.
- **Credential split.** This server holds the only `gmail.modify` credential; the
  agent reaches it through a static bearer and the agent's own Google token stays
  read-only. The agent cannot bypass the server to reach Gmail directly.

Enforcement is structural: it lives in reviewed code (`LabelClient`, checked
before any Gmail call), not in the agent's instructions. In Haku's security
model this is the one sanctioned world-write — <../base/SECURITY.md>.

## No human-in-the-loop

Because the surface is safe by construction, every call is auto-allowed — there is
no Airlock approval gate in front of this server. This is rung 2 ("filter the tool
surface") of Haku's safety doctrine (<../base/SECURITY.md>), not rung 3 (human-in-the-loop):
the surface is narrow enough that there is no decision left for a human to make, so an
Airlock predicate in front would be an always-`Approved` no-op.
