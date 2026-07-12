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
  presentation format here — those are Haku's implementation and live in its state
  (`haku-state`), not base.
- The **run procedure** (the imperative step-by-step a session executes) lives
  in `haku/run.md` (environment-neutral); per-environment entrypoints like
  `haku/runtime/claude_web_env/run.md` only layer setup and defer to it. Neither belongs
  in `base/` — if you find yourself writing "step 1, step 2…" here, it belongs in
  `haku/run.md`.
- `sources/<channel>.md` — one file per **information source (channel)**. Operator-owned
  sources are read-only; Haku-owned channels such as its mailbox may expose the scoped
  mutations granted by `instructions.md` → _Hard rules_. Source files document access
  mechanics — auth, API/query shape, fields, operations, gotchas — but never grant authority
  or prescribe what to do with the data.

Haku's **method is not in base.** The procedures it runs (the "passes"), the UI it serves,
and whatever format that UI presents (the current "items" board) live in Haku's state
(`haku-state`: `procedures/`, `ui/`, `items/`), owned by Haku. Don't add them here.

**Source vs. procedure — the boundary** (the thing that kept getting muddled): a source
file (base) is about **accessing** the data within the hard-rule authority inventory; a
procedure (Haku's state) is about **acting on** what you find. If a line reads "look for X → surface it," it's a procedure,
not a source — it belongs in haku-state's `procedures/`, not `base/sources/`. Keep
`sources/*.md` free of "look for / surface a finding" process, and keep procedures free of
channel-specific access mechanics (those belong in the source).

## Editing rules

- `base/` is read-only to Haku and baked into its image; behavior changes land
  by editing here and letting the image rebuild (Flux image automation bumps the
  CronJob tag). There is no live-editing path.
- Do **not** add seed content to Haku's state from `base/` — it's baked into the
  image and read-only. `base/` and state are separate; state is Haku's general durable
  write surface, while the complete bounded-write inventory lives in `instructions.md`
  → _Hard rules_. There is **no seed template**: `haku/state_template/` was retired 2026-07-07
  (nobody was scaffolding new instances, and the template had become an unmerged fork of
  the live `haku-state`). Method/UI changes that used to land as template edits now land
  as **specs** — describe the contract change (an endpoint, a widget, a request schema) in
  ducktape docs/plans and let Haku implement it in its own `ui/`/`procedures/`; don't edit
  Haku's UI code for it in ducktape.
- Keep `instructions.md` and `haku/run.md` in sync when you change the cycle: the
  contracts are described once in `instructions.md`; `haku/run.md` holds the **shape of
  the run** (ordering invariants + the fluid loop, not a rigid step list) and
  per-environment entrypoints only add setup — neither restates the contracts. Don't
  re-introduce a numbered 1..N waterfall.
