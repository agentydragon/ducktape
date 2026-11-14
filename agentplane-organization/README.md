# Agentplane Go organization plan

[Current and goal trees](index.html) are the review surface.

`go-plan.json` is the authored plan: package responsibilities, target owners,
remaining work, content prerequisites and adoption stages. `go-files.json` is
the complete source ledger at the same immutable commit. `evidence.json`
records source review, actual importers/build targets and report validation.
These files describe a proposal, not implemented boundaries.

`target_binaries` in `go-plan.json` records the agreed binary shape (an
Environments binary and a Sessions binary in place of the Runner Gateway and
Sandbox Controller), the cut line between them with each crossing's status, the
keep-alive hold design, and the operator questions still open, asked one at a
time. Tracks name it where their placement depends on it.

## Reading the goal

- `READY`: the selected whole-file move or package-name correction is decided;
  implementation still has to close its callers and pass checks.
- `PLANNED`: the responsibility and proposed owner are concrete; constructor,
  helper or test allocation still needs implementation review.
- `AGREED DIRECTION`: the operator agreed the move's direction; its contract
  (calls, state, serving) is written and reviewed before any file moves.
- `HELD`: a decided move that waits for a boundary decision so its files move
  once.
- `DESIGN REQUIRED`: the item states the unresolved transaction, state or
  lifecycle contract inside one component. Candidate directories are shown
  with `?`.
- `OPEN DIRECTION`: the move depends on a service decision nobody has made.
  Code under it stays where it is.
- `DEFERRED …`: an operator-deferred contract decision. Authentication and
  protobuf namespace choices stay separate from ordinary organization moves.
- `retain`: the package keeps a specific responsibility and dependency boundary.
  It remains in scope. Retention does not mean every file in it is permanently
  settled or that the package has passed behavior acceptance for a new design.

## Sequencing

Stages run in order of certainty. Stage 1 deletes leftovers; stage 2 fixes
layout in components no service decision touches; stage 3 follows directions
the operator agreed; stage 4 extracts responsibilities behind a local contract;
stage 5 waits on open service decisions; stage 6 holds deferred authentication
and protobuf contracts. A file moves or is renamed once: an in-component move
waits for any agreed or open boundary change that would move the same file
again. Line-level cleanups stay in the task DAG's cleanup section; this plan
only orders them before the moves that would carry them.

Exact mappings and candidate allocations are different. A ledger entry with no
selected file placement points to its package decision and work item; do not invent
a filename to make coverage appear complete. A ready command placement can precede a
later function extraction; when their source overlaps, the proposed function
allocation takes precedence in the goal tree and ledger. Package names, generated contract
names and filesystem paths are also distinct: a path cleanup does not authorize
renaming RPC methods or stored type URLs.

## Refresh and burn-down

1. Pin a current source commit and enumerate **all** tracked `agentplane/**/*.go`
   files, including tests, fixtures, experiments and tracked generated files.
   Read declarations/imports and hash exact Git blobs. Re-review new or changed
   responsibilities; an inherited `retain` label is not a new source audit.
2. Update the authored package decisions and remaining tracks. Include source
   locations, target owners, caller/BUILD/runfile closure, invariants, existing
   validation labels and the exact unresolved gate. Keep content dependencies
   separate from files that merely conflict during rebase.
3. After an implementation merges, remove its work item, completed mappings and
   satisfied prerequisite edges. Refresh the current tree and ledger. Reconsider
   the residual owner and next extraction; do not add a merged-work list.
4. Ensure the source SHA, file hashes/declarations, package rosters, dispositions,
   track links and evidence paths agree. Every changed package has a track;
   every retained package has a reason. Validate labels against the pinned BUILD
   files, distinguishing current labels from proposed relocated labels.
5. Check the report and shared home page in desktop/mobile and light/dark modes
   with real pinned CDN responses; exercise selectors, tree scrolling, package
   disclosures, work filters and links. Inspect the screenshots. Record those
   results separately from source behavior tests and live deployment evidence.

Keep source implementation PRs coherent, ideally 200–3,000 changed LOC after
rename detection, with tests and all callers in the same PR. Use native GitHub
Stacks for content-dependent PRs. The plan itself stays in this gh-pages report;
source code, runtime contracts and maintained operating guidance stay with their
owners on devel.
