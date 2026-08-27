---
name: plan_dag
description: Render the planned next actions and their dependencies as a living mermaid DAG artifact — open PRs, human decision gates, planned work, merged context. Use for orchestration/TPM status or any multi-step plan with human gates.
argument-hint: "<scope, e.g. haku-console>"
---

# Plan DAG

Publish one HTML artifact holding one mermaid flowchart of the plan: what is in
flight, what is planned, what blocks what, and — first-class — where a **human
action** is the unblocker. Keep it current: republish the same file path (same
URL) whenever a PR opens or merges, a gate resolves, or the plan resequences,
and date-stamp the subtitle each time.

The board shows pending and in-flight work, not history: **flush merged PRs off
the board on the update after they land** — the tracker and git are the archive
— naming the flushed PRs once in the subtitle so removals read as deliberate.
Their downstream nodes simply become roots. A completed non-PR node (a ruled
gate, a finished recon phase) may outlive its moment, but only while it anchors
fresh in-flight work; flush it once its outgoing edges stop explaining anything.

**Flush and restore against the primary source, never memory.** Before the
board asserts a PR's state — flushing it as merged, restoring it as open —
read the actual state (the API's `merged` field, or the squash commit on the
base branch). A remembered "delivered", a summary, and a stale listing all
lie, in both directions, and a board that mis-states PR state loses the only
thing it has. Corollary: review comments can materialize long after their
timestamps (a pending review is invisible until submitted), so an empty
thread listing never proves no review round is in flight.

## Node taxonomy

Four classes, styled explicitly (see gotchas), plus one hard shape rule:

```text
["#4691 grant principals · PR"]:::pr        open PR — GitHub outline green
["#4832 extract indexer · ready"]:::ready   PR ready to merge — solid green
{{"operator: #4667 verdict"}}:::gate        human decision gate — rust hexagon
["#4772 vocabulary collapse"]:::next        planned, undispatched — dashed
["#4710·0 fence recon ✓"]:::done            done phase / ruled gate — GitHub purple
```

PR states wear GitHub's colors (open green, merged purple, solid merge-button
green for "ready"), so PR standing reads without the legend. The done class is
transient by design: a merged PR wears it for at most the update that reports
the merge, then leaves the board (see the flush rule above); its steady-state
occupants are ruled gates and completed phases still anchoring fresh work.

- A gate names **whose** action unblocks it, compressed with a human glyph:
  `🧑 Codex canary`, `🧑 #4667 verdict` — never a vague "pending".
- Unicode over words where a glyph reads faster: `✓` suffix on merged nodes,
  `⛔` prefix on a blocking incident, `🧑` for human gates. A blocking incident
  gets its own class (GitHub danger red: `fill:#FFEBE9,color:#82071E,stroke:#CF222E`)
  and an edge into whatever it blocks.
- **Link every node that names an entity — the whole label, not just the
  token**: `["<a href='https://github.com/OWNER/REPO/pull/4691'>🔐 #4691 grant
principals · PR</a>"]` (single quotes inside the double-quoted label). Anchors
  survive the label sanitizer; inherit the state color, underline only on hover
  — the `#NNNN` already marks it as an entity:

  ```css
  .board .mermaid a {
    color: inherit;
    text-decoration: none;
  }
  .board .mermaid a:hover {
    text-decoration: underline;
  }
  ```

- Gates are hexagons `{{…}}`, **never rhombus `{…}`** — mermaid inflates
  diamonds to fit text diagonally and they dominate the layout.
- `done` nodes appear only where a live edge needs them as context; the chart
  is a burn-down, not an archive.
- An edge means "cannot start/land before", nothing softer — "touches the same
  file" is not an edge (AGENTS.md § Splitting Work Into PRs). Note the few
  deliberate serializations in prose under the chart, with why.
- **Edge direction is always dependency → dependent**: the arrow leaves the
  thing that must land first and enters the work waiting on it, so "no unmet
  incoming edge" ⇔ dispatchable, uniformly. An umbrella/tracking issue
  **depends on its parts landing** — parts point into it (it is a sink of its
  parts), never out of it. An umbrella fanning out to its parts reverses the
  read and makes ready work look gated; audit every edge against this rule
  before publishing.

## Graph structure

- **No workstream subgraphs.** Side-by-side subgraph boxes force one wide row
  and the whole SVG shrink-to-fits until text is unreadable. Leave workstreams
  as disconnected chains — dagre stacks components vertically on its own.
- **Workstreams as glyphs, not boxes.** Where workstream identity is worth
  showing, prefix each chain's node labels with a small per-workstream icon
  (🔐 💬 🗂 🤖 📸 …) and put the icon key in the legend — the grouping survives
  without the layout cost.
- Keep labels ≤ ~30 characters; `flowchart LR`; tighten with
  `%%{init: {"flowchart": {"nodeSpacing": 26, "rankSpacing": 40, "padding": 7}}}%%` — halving the default node padding is what gets the natural width under the container and the effective scale to 1.0.

## Mermaid-in-artifact gotchas

- **Explicit colors on every node.** The artifact viewer re-themes mermaid per
  viewer theme; `themeVariables` node colors get overridden (the
  black-text-on-dark-node bug). Only `classDef … fill:…,color:…,stroke:…`
  (and `style`/`linkStyle`) survive:

  ```text
  classDef pr fill:#DAFBE1,color:#116329,stroke:#1A7F37,stroke-width:1.6px
  classDef ready fill:#2DA44E,color:#FFFFFF,stroke:#1A7F37,stroke-width:1.6px
  classDef gate fill:#F6E8E1,color:#5A2E1D,stroke:#A4553B,stroke-width:1.4px
  classDef next fill:#F1EFE9,color:#1D1F1E,stroke:#8A9691,stroke-dasharray:5 3
  classDef done fill:#FBEFFF,color:#6639BA,stroke:#8250DF
  linkStyle default stroke:#2F6F5E,stroke-width:1.4px
  ```

- **Background: transparent beats a paper card when the fills allow it.** The
  GitHub state colors are label-chip pastels that read on light and dark
  grounds alike, so the diagram container can sit directly on the page
  background; make edges and arrowheads follow the page foreground with CSS
  (both themes then just work):

  ```css
  .board .mermaid svg .edgePaths path {
    stroke: var(--ink) !important;
  }
  .board .mermaid svg .marker {
    fill: var(--ink) !important;
    stroke: var(--ink) !important;
  }
  ```

  Verify BOTH themes in the local render (Playwright `colorScheme: "dark"`).
  Fall back to a fixed light card only when node fills genuinely fail on one
  ground.

- **Kill shrink-to-fit in CSS, not init.** `useMaxWidth:false` via `%%{init}%%`
  is not honored by the viewer; force natural size inside an
  `overflow-x: auto` card:

  ```css
  .board {
    overflow-x: auto;
  }
  .board .mermaid svg {
    max-width: none !important;
    height: auto;
  }
  ```

  If the card centers the diagram with flex, use `justify-content: safe
center` — plain `center` pushes an overflowing SVG's left edge into
  unscrollable space. And when the DAG outgrows the page column, raise the
  page `max-width` rather than letting the tail hide behind the card scroll:
  a node nobody sees is a node the board doesn't have.

- **Look at the render before shipping.** Locally: `npm i mermaid
playwright-core`, load the fragment plus
  `node_modules/mermaid/dist/mermaid.esm.min.mjs` in the preinstalled Chromium
  (`/opt/pw-browsers/chromium-*/chrome-linux/chrome`), screenshot, and check
  effective scale = SVG CSS width ÷ viewBox width ≥ ~0.9. Tiny-text reports
  come from skipping this.

## Iterate against the render, with the reader

The first version will be wrong in ways only pixels show. Render locally, look,
fix; then treat the reader's reactions ("text too tiny", "merged isn't distinct
from planned", "link the entities") as the review round they are — apply them to
the artifact **and** back into this skill when they generalize.

## Page skeleton

Title, one-subtitle line ("as of <date>Z (<latest event>)" + what the classes
mean), the board, a legend matching the four classes, then short notes:
the deliberate serializations, what each gate costs to open, and a
"not on this chart" line so scope reads as chosen.
