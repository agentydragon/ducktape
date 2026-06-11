# Peel CLI UX Notes

This note keeps generic usability follow-ups for `debundle peel` commands.
Corpus-specific paths and owner ids belong in the consuming repo.

## Bounded Planner Output

`debundle peel plan-work` is the main dispatch surface for agents and humans.
For fresh or sparse specs, output can still become hard to consume if proposal
details and diagnostics are both large.

Useful follow-ups:

- keep proposal output bounded by default when `--limit` is supplied
- expose summary counts even when details are truncated
- consider an explicit diagnostics toggle for first-pass planning
- keep sort keys documented and stable

## Concise Explain Mode

`debundle peel explain --owner-id ...` should have a compact mode focused on:

- selected owner identity and source span
- direct horizon status
- matching `plan-work` proposal, if any
- immediate constraining neighbors
- exact reason the owner is not landable today

Large companion-candidate structures should be opt-in when the caller is
debugging the planner itself.

## Source Roots

`source-slice --source-root ...` depends on the consuming target's source tree
layout. Runbooks and skills should make that target-specific root explicit
instead of assuming repository root or working directory.

## Patch Status Naming

`patch-status` is useful for intersecting binding-patch composition with graph
peelability. It is not the only way to discover readable peel candidates:
`candidates --readable-only` and `plan-work` may show graph-valid work even
when no whole patch section is ready.

Docs and skill text should avoid implying that an empty `patch-status` means
there is no landable work.
