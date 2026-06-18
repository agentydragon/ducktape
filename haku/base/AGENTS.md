@README.md

This file is for agents that **edit** `haku/base/`. It is not Haku's runtime
manual — that is `instructions.md`, which Haku reads as itself at run time. Don't
move runtime instructions here, and don't put editor notes in `instructions.md`.

## What lives where

- `instructions.md` — Haku's operating manual: who it is, how it reasons, the
  credential/perimeter model, hard rules, the item contract, the `items.md`
  spec, and tone. Durable, runtime-agnostic. Keep it at the "what Haku is and
  what it must honor" level.
- The **run procedure** (the imperative step-by-step a session executes) lives
  in the runtime entrypoint, `haku/claude_web_env/run.md`, not here. If you find
  yourself writing "step 1, step 2…" in `base/`, it belongs in the entrypoint.
- `schema/item.json` — the item schema, validated at write time. Changing item
  shape means editing this **and** the _Item contract_ / `items.md` spec in
  `instructions.md` together.
- `playbooks/` — **example** playbooks, explicitly not a closed set. When adding
  one, frame it as an example and keep it a pattern Haku adapts, not a mandate.

## Editing rules

- `base/` is read-only to Haku and baked into its image; behavior changes land
  by editing here and letting the image rebuild (Flux image automation bumps the
  CronJob tag). There is no live-editing path.
- Do **not** add seed content to Haku's state from here — `haku-state` starts
  empty and Haku creates its own structure. `base/` and state are separate; the
  only thing Haku writes is state.
- Keep `instructions.md` and the entrypoint in sync when you change the cycle:
  the contracts are described once in `instructions.md`; the entrypoint only
  sequences the steps and must not redefine them.
