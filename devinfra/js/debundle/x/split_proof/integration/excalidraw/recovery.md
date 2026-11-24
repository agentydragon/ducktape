# Recovering the pinned Excalidraw editor's authored modules

The canonical Rust editor graph places **272 of 1,787 units in 35 named modules**.
The browser smoke selects readable binding names from those owners. Each trial
keeps all other units in the original entry, uses Rust's selected schedule, and
requests the structural `normal-completion` guarantee at the browser origin.
A refusal of one placement does not establish that moving a binding changes
JavaScript behavior.

The [passing subset](../../../../excalidraw/editor_smoke_subset.json) moves **107 bindings
into 23 canonical modules**, up from 16 in four. The
[combined run](https://app.buildbuddy.io/invocation/6777e49b-e517-41bb-ba1e-b1b24dea0b85)
built Rust's selected tree and certified Lean's selected tree both at emission
and from disk. Chromium loaded the certified modules and compared the complete
editor entry namespace with the original. The result is a lower bound for this
checker and input; the candidate filters below are not a maximality proof.

## What limited the candidate set

To select a trial, join the canonical Rust graph to the scope-annotated original
editor by source ordinal. For each canonical unit, record its declaration kind,
module bindings it references (including in closures), and which bindings the
original entry exports. Apply the following filters in order. These are candidate
selection counts, not 158 independently rejected Lean runs.

| Filter                                               | Units | Example and reason                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------------------------------------- | ----: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Hoisted function declarations                        |    25 | Moving the full angle module reached `degreesToRadians`. The structural move rule expects a local allocating declaration; a hoisted function is already present before evaluation. [Lean refusal](https://app.buildbuddy.io/invocation/10de4651-3fb8-4976-9254-126dd29cafb6)                                                                                       |
| An earlier source unit refers to the binding         |    42 | `isFreeDrawElement` captures `isFreeDrawElementType`. The [move checker](../../lean/EsmSplit/CoreJS/LinkedPivotCheck.lean) requires earlier units to have no access to the moved region. A reference in a deferred body counts. [Lean refusal](https://app.buildbuddy.io/invocation/69a56768-5f93-452b-8a9d-8ea7727ea7fd)                                          |
| Needs a private binding that would stay in the entry |    91 | Iterate to a fixed point: if a selected unit needs a private entry binding whose owner cannot move with it, drop that unit and revisit its consumers. For example, moving `calculateScrollCenter` adds three editor exports; moving only `centerScrollOn` preserves the namespace. [Scroll export comparison](README.md#strict-certification-of-the-scroll-module) |

This left 114 candidates. The Rust gate and Lean then checked concrete subsets.
The following blockers came from those runs:

| Bindings                                                     | Observed blocker                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cachedCjkRegex`, `cachedLineBreakRegex`, `cachedEmojiRegex` | Rust requires each cache and the function that assigns it to share an owner. The 114-binding trial split all three pairs. The corresponding writers are `containsCJK`, `getLineBreakRegex` and `getEmojiRegex`; they have private dependencies in the entry. [Rust rejection and unit pairs](https://app.buildbuddy.io/invocation/92f3992e-746a-41b5-b040-148b68ca3755) |
| `resizeImageFile`                                            | The helper contains relative dynamic imports. The deployment check rejected moving it away from the original editor URL. [Lean refusal](https://app.buildbuddy.io/invocation/b76a3d3e-7ede-4690-ba11-978fcaafaede)                                                                                                                                                      |
| `COMMON`, `EMOJI`                                            | These text-wrapping regex tables did not pass the local-allocation check. [COMMON refusal](https://app.buildbuddy.io/invocation/70798918-ca88-4af1-8320-ebf95a3d2839) · [EMOJI refusal](https://app.buildbuddy.io/invocation/736a4877-db12-4d6d-b756-c220a4883473)                                                                                                      |
| `isLinearElement`                                            | Its region is already reachable in the initial linked state. A function declaration later in source order captures this binding, so checking earlier source references alone did not screen it out. [Lean refusal](https://app.buildbuddy.io/invocation/4c804fb2-54b2-4118-a811-f5f29bf3261d)                                                                           |
