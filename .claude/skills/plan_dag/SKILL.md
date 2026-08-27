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

## Node taxonomy

Four classes, styled explicitly (see gotchas), plus one hard shape rule:

```text
["#4691 grant principals · PR"]:::pr     open PR — solid accent border
{{"operator: #4667 verdict"}}:::gate     human decision gate — hexagon
["#4772 vocabulary collapse"]:::next     planned, undispatched — dashed
["#4850 clipping removal ✓"]:::done      merged context — muted
```

- A gate names **whose** action unblocks it and what the action is
  (`operator: Codex canary`), never a vague "pending".
- Gates are hexagons `{{…}}`, **never rhombus `{…}`** — mermaid inflates
  diamonds to fit text diagonally and they dominate the layout.
- `done` nodes appear only where a live edge needs them as context; the chart
  is a burn-down, not an archive.
- An edge means "cannot start/land before", nothing softer — "touches the same
  file" is not an edge (AGENTS.md § Splitting Work Into PRs). Note the few
  deliberate serializations in prose under the chart, with why.

## Graph structure

- **No lane subgraphs.** Side-by-side subgraph boxes force one wide row and the
  whole SVG shrink-to-fits until text is unreadable. Leave workstreams as
  disconnected chains — dagre stacks components vertically on its own.
- Keep labels ≤ ~30 characters; `flowchart LR`; tighten with
  `%%{init: {"flowchart": {"nodeSpacing": 30, "rankSpacing": 45}}}%%`.

## Mermaid-in-artifact gotchas

- **Explicit colors on every node.** The artifact viewer re-themes mermaid per
  viewer theme; `themeVariables` node colors get overridden (the
  black-text-on-dark-node bug). Only `classDef … fill:…,color:…,stroke:…`
  (and `style`/`linkStyle`) survive:

  ```text
  classDef pr fill:#E5F0EB,color:#173B31,stroke:#2F6F5E,stroke-width:1.6px
  classDef gate fill:#F6E8E1,color:#5A2E1D,stroke:#A4553B,stroke-width:1.4px
  classDef next fill:#F1EFE9,color:#1D1F1E,stroke:#8A9691,stroke-dasharray:5 3
  classDef done fill:#EAE8E2,color:#6B7370,stroke:#C1BFB8
  linkStyle default stroke:#2F6F5E,stroke-width:1.4px
  ```

- **Fixed paper card.** Because the SVG renders once with fixed colors, put it
  on a card with an explicit light background that holds in both page themes;
  keep the page chrome theme-aware around it.
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

- **Look at the render before shipping.** Locally: `npm i mermaid
playwright-core`, load the fragment plus
  `node_modules/mermaid/dist/mermaid.esm.min.mjs` in the preinstalled Chromium
  (`/opt/pw-browsers/chromium-*/chrome-linux/chrome`), screenshot, and check
  effective scale = SVG CSS width ÷ viewBox width ≥ ~0.9. Tiny-text reports
  come from skipping this.

## Page skeleton

Title, one-subtitle line ("as of <date>Z (<latest event>)" + what the classes
mean), the board, a legend matching the four classes, then short notes:
the deliberate serializations, what each gate costs to open, and a
"not on this chart" line so scope reads as chosen.
