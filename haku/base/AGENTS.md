@README.md

This file is for agents that **edit** `haku/base/`. It is not Haku's runtime
manual — that is `instructions.md`, which Haku reads as itself at run time. Don't
move runtime instructions here, and don't put editor notes in `instructions.md`.

## What lives where

- `instructions.md` — Haku's operating manual: who it is, how it reasons, the
  credential/perimeter model, hard rules, the item contract, the dashboard spec,
  and tone. Durable, runtime-agnostic. Keep it at the "what Haku is and
  what it must honor" level.
- The **run procedure** (the imperative step-by-step a session executes) lives
  in `haku/run.md` (environment-neutral); per-environment entrypoints like
  `haku/runtime/claude_web_env/run.md` only layer setup and defer to it. Neither belongs
  in `base/` — if you find yourself writing "step 1, step 2…" here, it belongs in
  `haku/run.md`.
- `schema/item.json` — the item schema, validated at write time. Changing item
  shape means editing this **and** the _Item contract_ / _Dashboard_ spec in
  `instructions.md` together.
- `sources/<channel>.md` — one file per **information source (channel)**: gmail,
  calendar, drive, tasks, tana, plaid, ducktape. Content is strictly **what this
  channel tells you about the operator + how to read it** — auth, API/query shape,
  fields, gotchas. **Not** what to do with it.
- `recipes.md` — **source-agnostic techniques** (the _process_): reusable ways to be
  useful, applied situationally across whatever channels fit — inbox-like triage &
  cleanup, calendar prep, financial anomalies & leaks, delegation scan, opportunistic
  synthesis, … Illustrations, **not** a checklist or a closed set. Read by Haku as part
  of its manual (referenced from `instructions.md` → _How you reason_).

  **Source vs. recipe — the boundary** (the thing that kept getting muddled): a source
  file is about **getting and reading** the data; a recipe is about **acting on** what
  you find. If a line reads "look for X → file an item," it's a recipe, not a source —
  move it. Keep `sources/*.md` free of "look for / file one item per finding" process,
  and keep `recipes.md` free of channel-specific access mechanics (those belong in the
  source). A recipe should name the channels it applies over, not embed how to query them.

## Editing rules

- `base/` is read-only to Haku and baked into its image; behavior changes land
  by editing here and letting the image rebuild (Flux image automation bumps the
  CronJob tag). There is no live-editing path.
- Do **not** add seed content to Haku's state from `base/` — it's baked into the
  image and read-only. First-run starter scaffolding lives in `haku/state_template/`
  instead (a separate ducktape copy-source Haku reads at run time, not part of
  `base/`); Haku copies it, so state stays Haku-authored. `base/` and state are
  separate; the only thing Haku writes is state.
- Keep `instructions.md` and `haku/run.md` in sync when you change the cycle: the
  contracts are described once in `instructions.md`; `haku/run.md` holds the **shape of
  the run** (ordering invariants + the fluid loop, not a rigid step list) and
  per-environment entrypoints only add setup — neither restates the contracts. Don't
  re-introduce a numbered 1..N waterfall.
