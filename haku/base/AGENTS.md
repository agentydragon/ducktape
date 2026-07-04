@README.md

This file is for agents that **edit** `haku/base/`. It is not Haku's runtime
manual — that is `instructions.md`, which Haku reads as itself at run time. Don't
move runtime instructions here, and don't put editor notes in `instructions.md`.

## What lives where

- `instructions.md` — Haku's operating manual: who it is, its objective, how it reasons,
  the credential/perimeter model, hard rules, continuity, and that it maintains its own
  UI/procedures/method. Durable, runtime-agnostic, **and item-agnostic** — it must read the
  same no matter what Haku's state contains. Keep it at the "what Haku is and what it must
  honor" level. **Do not** put an item schema, a board spec, statuses, action kinds, or any
  presentation format here — those are Haku's implementation and live in its state (seeded
  from `haku/state_template/`), not base.
- The **run procedure** (the imperative step-by-step a session executes) lives
  in `haku/run.md` (environment-neutral); per-environment entrypoints like
  `haku/runtime/claude_web_env/run.md` only layer setup and defer to it. Neither belongs
  in `base/` — if you find yourself writing "step 1, step 2…" here, it belongs in
  `haku/run.md`.
- `sources/<channel>.md` — one file per **information source (channel)**: gmail,
  calendar, drive, tasks, tana, plaid, ducktape. Content is strictly **what this
  channel tells you about the operator + how to read it** — auth, API/query shape,
  fields, gotchas. **Not** what to do with it.

Haku's **method is not in base.** The procedures it runs (the "passes"), the UI it serves,
and whatever format that UI presents (the starter's "items" board) are seeded
from `haku/state_template/` (`procedures/`, `ui/`, `items/`) and owned by Haku
in its state. Don't add them here.

**Source vs. procedure — the boundary** (the thing that kept getting muddled): a source
file (base) is about **getting and reading** the data; a procedure (Haku's state) is about
**acting on** what you find. If a line reads "look for X → surface it," it's a procedure,
not a source — it belongs in `state_template/procedures/`, not `base/sources/`. Keep
`sources/*.md` free of "look for / surface a finding" process, and keep procedures free of
channel-specific access mechanics (those belong in the source).

## Editing rules

- `base/` is read-only to Haku and baked into its image; behavior changes land
  by editing here and letting the image rebuild (Flux image automation bumps the
  CronJob tag). There is no live-editing path.
- Do **not** add seed content to Haku's state from `base/` — it's baked into the
  image and read-only. First-run starter scaffolding lives in `haku/state_template/`
  instead (a separate ducktape copy-source Haku reads at run time, not part of
  `base/`); Haku copies it, so state stays Haku-authored. `base/` and state are
  separate; the only thing Haku writes is state.
- When syncing Haku's evolved method back into `haku/state_template/`, keep it a
  **generic, person-agnostic starter** — carry the structural/high-level changes
  (architecture, the surfaces every instance wants, the deploy pipeline, generic
  procedures/schema) but **never the operator's personal specifics** (their items,
  `memory/` content, logs, or surfaces built around one operator's accounts/life). The
  principle and the "would it help an arbitrary new operator?" test live in
  [`state_template/README.md`](../state_template/README.md) → _Principle: a generic
  starter_.
- Keep `instructions.md` and `haku/run.md` in sync when you change the cycle: the
  contracts are described once in `instructions.md`; `haku/run.md` holds the **shape of
  the run** (ordering invariants + the fluid loop, not a rigid step list) and
  per-environment entrypoints only add setup — neither restates the contracts. Don't
  re-introduce a numbered 1..N waterfall.
