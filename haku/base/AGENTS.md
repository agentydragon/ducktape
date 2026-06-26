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
- `sources/` — Haku's **information sources** (operator-linked channels: inbox,
  calendar, Drive, Tana, repos, …) plus a couple of example cross-source techniques.
  They are inputs/reference, **not** a checklist — frame each as "what this source
  tells you + how to read it," and keep techniques as illustrations Haku adapts, never
  a mandate or a closed set. Don't let this dir read as "runbooks to execute."

## Editing rules

- `base/` is read-only to Haku and baked into its image; behavior changes land
  by editing here and letting the image rebuild (Flux image automation bumps the
  CronJob tag). There is no live-editing path.
- Do **not** add seed content to Haku's state from here — `haku-state` starts
  empty and Haku creates its own structure. `base/` and state are separate; the
  only thing Haku writes is state.
- Keep `instructions.md` and `haku/run.md` in sync when you change the cycle: the
  contracts are described once in `instructions.md`; `haku/run.md` only sequences
  the steps (and per-environment entrypoints only add setup) — neither redefines
  the contracts.
