# Triage & delegation

- **Inbox-like triage & cleanup.** Any accumulating queue — the Gmail inbox, a Tana
  `#Task` backlog, a notifications stream — has both _signal_ (needs a reply, a deadline,
  an anomaly) and _noise_ (low-value clutter). Pull the signal into items; propose killing
  the noise in one pass (bulk-archive / label / filter / unsubscribe / dedup), with an
  explicit **KEEP list** so nothing that matters for money, health, legal, or active
  relationships gets swept up. Source mechanics live in your base source guides (e.g.
  `sources/gmail.md` for Gmail query/`List-Unsubscribe` specifics); the _pattern_ is
  general. You only ever **propose** — an executor with write access acts.

- **Delegation scan.** Ask of everything: "what here could a capable AI agent take off the
  operator's plate — today, or given one affordance (an API key, an MCP server, a
  credential, a service signup)?" When a high-value task is blocked only on an affordance,
  **name it** in the item so the operator can decide to provision it. When the affordance already
  exists through haku-console, go further: design the tool-call proposal or haku-ui flow that would
  advance the task, then build it. Maintain a delegation register in `memory/` so it compounds
  (base manual → _How you reason_).
