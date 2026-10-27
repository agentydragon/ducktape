# Thread frontend

`home.tsx` owns the Threads landing page, and `thread_list.tsx` owns the grouped
Thread list used there and in the sidebar. `sandbox_threads.tsx` and `new_thread_form.tsx`
own the Thread list and creation form on a Sandbox. `thread_groups.ts`, `thread_notices.tsx`,
the status indicator, and the Thread tab favicon belong with that navigation.

`page.tsx` resolves the selected Thread and its live availability. `conversation.tsx`
connects synchronization with command delivery; `composer.tsx` owns the draft, keyboard handling,
and send/interrupt controls; `history_row_views.tsx` renders history rows,
and `virtualized_history.tsx` owns their virtualization and scroll anchoring. `thread_commands.tsx`
owns local delivery, retries, and command outcome presentation. `thread_sync.ts`
defines the view-facing contract, and `electric_sync.tsx` implements it with
Electric. `thread_cards.tsx` renders bodies and cards, while `thread_evidence.tsx`
owns their lazy evidence paging. History rows, local commands, disclosures, Thread
status, title, chronological debug, Thread API calls, and the shell's shared Thread listing stream belong to this feature. The shared sidebar shell, HTTP transport, Sandbox status model, and generic rendering components stay in the parent frontend package.

The Bazel targets live in `BUILD.bazel`. The ambient CSS and per-icon TypeScript
declarations are local copies of the parent package declarations: each TypeScript
target compiles its own sources, and the Bazel rule cannot list a source from another
package. Keep those declarations in sync when changing them. The feature
extractions do not change the thread sync or scrolling contracts; see `../SPEC.md` for reader-facing scroll
guarantees, `scrolling_verification.md` for engineering checks, and
`../../../docs/thread_view_sync.md` for sync design.

## Disclosure layout ownership

The shared primitives live in `../disclosure.tsx` and `../disclosure.css`:

- `DisclosureCard` owns the outer frame. Its context selects outer/nested variants;
  consumers do not remove ancestor borders with descendant selectors.
- `Disclosure` computes a section inset once from the shared gutter, depth, and
  indent-step tokens. Its label and `DisclosureBody` consume that same inset.
  A body wraps terminal content; nested disclosures stay outside it so frames
  remain full-width. Independent bodies represent independent content sections.
- Interactive and static headings share a CSS grid. The accordion button spans
  the whole row; its label and chevron use subgrid tracks shared with the separate
  action slot. Intrinsic action width determines the tracks without measurements,
  absolute offsets, or compensating label padding.
- CSS owns row-height policy. Runs select the shared compact collapsed-height
  token. JavaScript measures actual heading height only to add ancestor heights
  into sticky offsets, including wrapped headings. Hidden clamp headings are an
  explicit `Disclosure` state, not an ancestor CSS override.
- `CodeBlock` owns its framed/plain surface policy, shared by its placeholder
  and editor. Plain code receives its gutter from `DisclosureBody`; callers do
  not undo editor borders or padding. Label/muted code remain text presentations.
- `EvidenceToggle` explicitly selects inline, overlay, or bubble placement.
  Heading geometry belongs to the shared grid, while evidence owns visibility.

Gutter and hierarchy design changes belong in the shared policy tokens. Keep
browser checks for actual text/heading alignment, full-row hit areas, action and
chevron separation, stable clamp expansion, and nested sticky collapse behavior.

## Rollout visual fixture provenance

`../harness/realistic_rollout.json` and `../harness/reported_rollout.json` are layout fixtures
based on staging metadata: event kinds and order, completion/failure state, and text size and line
count. Their prose, command arguments, and outputs are newly authored safe stand-ins, not copied
transcripts or evidence of realistic command semantics. The reported excerpt shows completed items
even though its source thread had an active feed, so it does not reproduce that active state.
