# The knowledge garden — where detail lives, interlinked

The UI's 🌱 **Garden** tab renders any markdown I keep under the whitelisted dirs (`memory/`,
`procedures/`, `runs/`) as MDX, with working cross-links. It's my notebook made browsable — and the
**default home for detail**.

## Convention: put detail in interlinked garden files, not in structured fields

Structured surfaces (an `items/` card, a run manifest's `propagation[]`) are the **index** — terse,
scannable, machine-checkable. The **depth** belongs in a garden markdown file that the index links
to. So:

- When a task / topic / decision needs more than a couple of lines, write the detail as a
  `memory/<topic>.md` (or `runs/<…>.md`) file and **link to it** from the item/run that surfaced it,
  rather than stuffing prose into a YAML field.
- **Interlink generously.** A run note links the procedure it walked and the memory note it updated;
  a research thread links its sources and the item it backs; a topic note links related topics. The
  value compounds — the garden becomes a navigable web, not a pile of isolated files.
- Keep each file single-purpose and stably named so links stay valid across runs.

## Link syntax (what actually navigates)

Plain markdown links, resolved relative to the file they're in:

- `[kitchen checklist](../procedures/propagation/kitchen.md)` — relative path → opens in the garden.
- `[situational awareness](/memory/situational-awareness.md)` — leading `/` = repo root.
- `[sibling](other-note.md)` — bare name → same directory.
- `https://…` / `mailto:…` — external, opens in a new tab.

Only `.md`/`.mdx` links under the whitelisted dirs navigate in-app; everything else is a normal link.
A run note's links open the same garden viewer (the Runs tab deep-links into it).

## Standard widgets (MDX)

Authored content may embed the fixed widget set (see `ui/frontend/src/widgets.tsx`): `<Callout>`,
`<StatusBadge>`, `<PropagationMatrix data={…}/>`. Plain markdown is valid MDX, so notes that use no
widgets render unchanged. The widget registry is the only app surface authored content can reach.
