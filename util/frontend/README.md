# Transcript UI

`Markdown` and `CodeBlock` render untrusted prose and read-only code.
`UserMessage` supplies the user bubble, gutter and overlay slots; `VerbatimText`
preserves typed text. Applications supply message content and metadata.

`Disclosure`, `DisclosureCard`, `DisclosureBody` and `ClampedBlock` coordinate
nested headings and expandable content. `RetainedDisclosureProvider` keeps bounded
expansion state when virtualized rows unmount. Applications choose the groups,
summaries, stable disclosure IDs and default expansion. Key the provider by the
selected conversation/captured version when choices should reset on navigation.

`VirtualizedHistory` accepts keyed, revisioned rows and a `renderRow` function.
It owns virtualization, measurement, reader anchoring and scroll writes. Applications
own decoding, grouping, fetching, loading/error UI and any provisional tail.

- `scopeId` scopes remembered heights and the mounted viewport's lifetime. Include
  a captured version when its contents must have independent state.
- Change a row's `revision` when its contents change; retain its `key` when extending
  an existing group. `anchorKeys` map stable content identities to their current row.
- Choose `initialPosition` and `followTail` explicitly. Agentplane opens at the end
  and follows; the archive opens at the start and preserves reading during append
  pagination. `showJumpToLatest` controls the explicit return-to-tail button.
- `earlier` offers optional prepend loading near the start; `tail` can contain
  append-pagination controls. The library owns neither API nor cursor semantics.
- `diagnostics` optionally receives geometry and interaction events. Agentplane
  adapts these into its own opt-in recorder; the shared library installs no recorder.

The viewport exposes `data-scroll-mode` and `data-layout-settled` for browser
checks. A settled layout has measured the mounted rows, reconciled the reader's
position and finished loading their marked (`aria-busy`) content.
