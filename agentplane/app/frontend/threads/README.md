# Thread conversation frontend

The Thread route lives here: `projected_session.tsx` presents the conversation and
the composer. `thread_commands.tsx` owns local delivery, retries, and command
outcome presentation. `thread_sync.ts` defines the view-facing contract, and
`thread_store.tsx` implements it with Electric. `thread_cards.tsx`
renders bodies and cards, while `thread_evidence.tsx` owns their lazy evidence
paging. History rows, local commands, disclosures, thread title, and
chronological debug belong to this feature. Shared shell, API client, and generic
rendering components stay in the parent frontend package; the sidebar's grouping logic stays beside its caller.

The Bazel targets live in `BUILD.bazel`. The ambient CSS and per-icon TypeScript
declarations are local copies of the parent package declarations: each TypeScript
target compiles its own sources, and the Bazel rule cannot list a source from another
package. Keep those declarations in sync when changing them. The feature
extractions do not change the thread sync or scrolling contracts; see `../SPEC.md` for reader-facing scroll
guarantees, `scrolling_verification.md` for engineering checks, and
`../../../docs/thread_view_sync.md` for sync design.
