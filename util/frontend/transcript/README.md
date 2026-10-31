# Transcript UI

[Markdown and code rendering](../README.md), disclosures and clamped content are
shared frontend primitives in the parent package.
`UserMessage` supplies the user bubble, gutter and overlay slots; `VerbatimText`
preserves typed text. Applications supply message content and metadata.

`StepLine` renders one compact step title and preview with optional disclosed content.
Applications choose which records form steps and own their retained open state.
The `transcript-steps` container packs collapsed steps together while leaving space
after an opened step.
`TextBlock` renders labeled, syntax-highlighted text in a bounded block; its
`expansion` prop accepts caller-owned state, and JSON objects or arrays are highlighted
as JSON when no language is supplied.

`InspectionToggle` supplies the compact source/debug action used by both applications.
A `transcript-inspection-owner` contains the action; bubble/overlay placements add
no row height. Hover, keyboard focus or the application-owned
`data-inspection-revealed` touch marker reveals it. Applications own inspection
content and can retain its open state with `useRetainedDisclosure`.

`RetainedDisclosureProvider` keeps bounded expansion state when virtualized rows unmount. Applications choose the groups,
summaries, stable disclosure IDs and default expansion. Key the provider by the
selected conversation/captured version when choices should reset on navigation.

`VirtualizedHistory` accepts keyed, revisioned rows and a `renderRow` function.
It owns virtualization, measurement, reader anchoring and scroll writes. Applications
own decoding, grouping, fetching, loading/error UI and any provisional tail.

- `scopeId` scopes remembered heights and the mounted viewport's lifetime. Include
  a captured version when its contents must have independent state.
- Change a row's `revision` when its contents change; retain its `key` when extending
  an existing group. `anchorKeys` map stable content identities to their current row.
  Mark nested semantic landmarks with `data-history-anchor-key` and include those
  stable, scope-unique keys in `anchorKeys`; while reading, the viewport anchors the
  deepest visible landmark at the viewport's top edge and can recover it after a React
  remount.
- History opens at the bottom and follows the tail until the reader scrolls up.
  `showJumpToLatest` controls the explicit return-to-tail button.
- `earlier` offers optional prepend loading near the start; `tail` holds provisional
  content or an empty state. The library owns neither API nor cursor semantics.
- `diagnostics` optionally receives geometry and interaction events. Agentplane
  adapts these into its own opt-in recorder; the shared library installs no recorder.

The viewport exposes `data-scroll-mode` and `data-layout-settled` for browser
checks. A settled layout has measured the mounted rows, reconciled the reader's
position and finished loading their marked (`aria-busy`) content.
