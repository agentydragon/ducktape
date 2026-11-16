# Plan assignments and seed worker tasks

Use the project's [adapter inputs](../skills/shared/workflow.md#adapter-contract)
and the [task chooser](../README.md#choose-a-task) to scope the work. Planning
and intake are read-only; each hands evidence to a worker who validates the edit.

When a large assignment rejects, use the shared workflow's
[passing residual baseline and divide-and-conquer strategy](../skills/shared/workflow.md#start-from-passing-residual-output-and-isolate-failing-assignments).
Retain useful passing subsets and isolate conflicting additions before turning
a broad rejection into a long interpreter worklist. An empty extraction spec
keeps every source unit residual in its original chunk; failure of that normal
baseline needs a tool/input investigation.

## Plan an assignment

Use the adapter's current graph, matching modules tree, source, and reports.
Planning is read-only; a query may evaluate a candidate's gate without applying it.
Start with `graph-summary`, then use `describe` and `inspect-source --id` before
recommending an assignment. Use `coverage` for ownership and `atoms` for the
complete source-unit groups the current analysis requires to share a destination.
Separate groups are not necessarily independently extractable; see [structural
atoms and extraction candidates](cli.md#structural-atoms-and-extraction-candidates)
before treating a residual inventory as a peel verdict.
`describe` defaults to 20 rows per section. Check its truncation metadata and use
`--all` when deriving a complete assignment or dependency closure from its rows.

For a `possible_getter` reason, inspect the lookup at `lookup.lexical_owner`
and its byte span, then the body identified by `promotion.source_owner` when
the promotion kind is `lazy_owner`. A promoted binding read does not imply
that the binding itself is an accessor. `raw_callbacks` identifies no single
body, and a retained witness does not enumerate every contributor. Check the
other reasons and reciprocal paths between the same destinations before
claiming that resolving this lookup would break a cycle. The witness format is
specified in [binding-ordering effect provenance](wire_format.md#binding-ordering-effect-provenance).

Choose the operation from the task chooser:

- For an explicit destination batch, run `modules check --batch changes.json`
  against the same graph and context. Include all jointly required source units
  and the actual destination, including an existing owner. This checks the whole
  assignment without editing selectors or spec files; graph acceptance does not
  imply the applicator can yet realize every shape. Recheck combined batches.
- For residual extraction, inspect the graph and source first, then use [exact
  residual placement](module_search.md) with the default exact `modules
propose` strategy. Review the complete mapping and destinations, check the plan,
  preview and apply that same plan, then regenerate and run behavior checks. A
  `found` result is graph acceptance only; selector resolution and runtime
  behavior are separate. `exhausted_no_peel` is scoped to its reported graph
  constraints and permitted moves. Budget exhaustion is incomplete. The
  Send the exact accepted plan and source evidence to the worker; changing its
  destinations needs another complete check.
- For existing claims, review the source units and choose semantic destinations,
  then use `modules check --batch` to validate the complete assignment before
  applying it with `modules refine`. Hand the reviewed source-unit map directly to
  a worker.
- For selector work, follow the [bulk conversion loop](selectors.md#bulk-conversion-loop).
  Give workers explicit items or coherent module scopes. Record repeated synthesis
  gaps as tooling work instead of assigning exact-body transcription.

Return the graph/input identity, reviewed units or proposals, source evidence,
destinations, selector anchors, candidate verdict, and unresolved questions.
Keep planning reports distinct from the operation's edit-batch format; the linked
CLI recipe defines that format.

## Prepare a residual-placement handoff

Use short source slices to infer purpose from implementations, callers, stable
strings/properties, and source proximity. If choosing a destination requires
broad architectural discovery, mark low confidence and route to the architect.

An exact placement result is one snapshot-bound plan, including jointly
required atoms and all destinations. Keep the plan intact as one worker packet;
add semantic names and source evidence separately. Splitting or combining its
changes requires checking the resulting complete assignment again. A graph
witness still needs selector realization, materialization and behavioral
evidence. `incomplete` is not a blocked-code or no-placement conclusion, and
`exhausted_no_peel` covers only the request's recorded owner policy,
destination set, required units and other scope.

Track source units by the current graph's unit IDs and bindings by chunk/name,
with the source/spec revisions that produced them. Unit IDs are scoped to one
captured input program; refresh the graph before handing off a changed spec.
